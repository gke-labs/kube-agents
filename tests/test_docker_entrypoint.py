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

import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import yaml

_REPO = pathlib.Path(__file__).resolve().parents[1]
_ENTRYPOINT = _REPO / "deploy" / "shared" / "docker-entrypoint.sh"
_CHAT_TEMPLATE = _REPO / "agents" / "chat" / "config.yaml"
# The operator's rendered ConfigMap, as the manifests golden records it. It is the only
# place in this repository where the entrypoint's expectation of the render can be
# checked against the render itself — the two are written in different languages.
_MANIFEST_GOLDEN = (
    _REPO
    / "k8s-operator"
    / "internal"
    / "testing"
    / "testdata"
    / "platform"
    / "expected"
    / "platformagent.yaml"
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
            full_env = {
                "PATH": "/usr/bin:/bin",
                "PLATFORM_AGENT_HOME": str(home),
                # These cases are about the GATE, and none of them seeds a config.yaml,
                # so every non-owner would otherwise sit in the wait below the gate for
                # its full default. Zero keeps them measuring what they are named for;
                # ConfigWaitTest owns the wait itself.
                "AGENT_SHARED_STATE_WAIT_SECS": "0",
            }
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

    def test_the_setup_creates_a_group_accessible_scratch_dir(self):
        """Step 4.5: the `gh --body-file` handoff directory must exist,
        group-accessible, before any script lazily creates it with whatever
        umask that process happens to carry. The sandbox (uid 10000) stages
        body files there and the credential sidecar (uid 10001) reads them
        through the shared fsGroup — group bits are the whole bridge since the
        #955 uid split; #1030 is what their absence looks like."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "data"
            proc = subprocess.run(
                ["sh", str(_ENTRYPOINT), "echo", "hermes", "gateway", "run"],
                capture_output=True,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "PLATFORM_AGENT_HOME": str(home),
                    "AGENT_SHARED_STATE_WAIT_SECS": "0",
                },
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            scratch = home / "scratch"
            self.assertTrue(scratch.is_dir(), "the setup must create scratch/")
            mode = scratch.stat().st_mode
            self.assertEqual(
                mode & 0o070,
                0o070,
                f"scratch/ is {oct(mode & 0o777)}: the sidecar reaches it only "
                "through the group bits",
            )

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


class ConfigWaitTest(unittest.TestCase):
    """The non-owner's bounded wait for the config.yaml the owner seeds.

    This replaced a subPath mount that shadowed the PVC copy on every volume, so the
    dashboard read a different config from the gateway's. In the shipped image the wait
    is belt and braces rather than the guarantee: upstream's stage2 hook seeds
    config.yaml from cli-config.yaml.example before the entrypoint runs, so the loop
    never iterates there. It is carried for an upstream that stops doing that, which is
    exactly why it needs tests of its own — nothing in the running system would notice
    if it broke. What they hold is that it waits, stops early, and never becomes a wedge.
    """

    def _run(self, *, seed_after=None, wait_secs=30, home_exists=True, timeout=None):
        """Run the entrypoint as an explicit non-owner against a home with no config.

        `seed_after`, in seconds, writes config.yaml from a timer thread — the owner
        container arriving late, which is the whole case. Returns `(proc, elapsed)`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "data"
            if home_exists:
                home.mkdir(parents=True)
            config = home / "config.yaml"
            timer = None
            if seed_after is not None:
                timer = threading.Timer(seed_after, lambda: config.write_text("model: {}\n"))
                timer.start()
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    ["sh", str(_ENTRYPOINT), "echo", "hermes", "dashboard"],
                    capture_output=True,
                    text=True,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "PLATFORM_AGENT_HOME": str(home),
                        "AGENT_SHARED_STATE_SETUP": "skip",
                        "AGENT_SHARED_STATE_WAIT_SECS": str(wait_secs),
                        # The wait is gated on this: it marks an operator-managed pod,
                        # the only arrangement where a second container is coming to
                        # seed the file. Without it here every test below would pass by
                        # never reaching the code it names. The path is never read —
                        # this branch execs long before the managed-scope assertion.
                        "HERMES_MANAGED_DIR": "/etc/hermes",
                    },
                    timeout=timeout if timeout is not None else wait_secs + 60,
                )
            finally:
                if timer is not None:
                    timer.cancel()
            return proc, time.monotonic() - started

    def test_a_config_already_there_is_not_waited_for(self):
        """The steady state — every restart on an existing volume — must not pause."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "data"
            home.mkdir(parents=True)
            (home / "config.yaml").write_text("model: {}\n")
            proc = subprocess.run(
                ["sh", str(_ENTRYPOINT), "echo", "hermes", "dashboard"],
                capture_output=True,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "PLATFORM_AGENT_HOME": str(home),
                    "AGENT_SHARED_STATE_SETUP": "skip",
                    # Long enough that waiting at all would blow the timeout below.
                    "AGENT_SHARED_STATE_WAIT_SECS": "600",
                    # Set, so that what skips the wait here is the file being present
                    # and not the operator gate — that gate has its own test.
                    "HERMES_MANAGED_DIR": "/etc/hermes",
                },
                timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("waiting up to", proc.stderr)
        self.assertIn("hermes dashboard", proc.stdout)

    def test_the_wait_ends_as_soon_as_the_config_appears(self):
        """Not a fixed sleep. The owner's seed has to release it early."""
        proc, elapsed = self._run(seed_after=2, wait_secs=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("waiting up to", proc.stderr)
        self.assertIn("appeared after", proc.stderr)
        self.assertIn("hermes dashboard", proc.stdout)
        self.assertLess(
            elapsed,
            30,
            "the wait polls for the file; taking the full budget with the file present "
            f"means it is really a sleep. stderr was:\n{proc.stderr}",
        )

    def test_a_config_that_never_arrives_starts_the_process_anyway(self):
        """Bounded, and it proceeds either way.

        Exiting here would only buy a kubelet backoff loop — this container carries no
        probes — and an owner that never runs is legitimate for a pre-populated volume.
        """
        proc, _ = self._run(wait_secs=2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARN", proc.stderr)
        self.assertIn("still absent", proc.stderr)
        self.assertIn(
            "hermes dashboard",
            proc.stdout,
            "the timeout must hand over to the command regardless; a non-owner that never "
            "starts is worse than one that starts early",
        )

    def test_a_non_numeric_budget_warns_and_falls_back(self):
        """`set -e` is on, so an unguarded `-lt` on a typo would kill the container.

        That is the exact outcome the branch exists to avoid, arriving through the knob
        that configures avoiding it. Seeded from a timer so the 120s fallback the guard
        installs is left early rather than waited out.
        """
        proc, _ = self._run(seed_after=1, wait_secs="two minutes", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARN", proc.stderr)
        self.assertIn("non-numeric", proc.stderr)
        self.assertIn("waiting up to 120s", proc.stderr)
        self.assertIn("hermes dashboard", proc.stdout)

    def test_a_home_that_does_not_exist_yet_is_waited_through_not_crashed_on(self):
        """The owner creates $TARGET_DIR itself, so on a fresh PVC it is absent, not empty.

        `set -e` is on and the `cd` below this point is guarded for exactly this reason;
        the wait must not become the thing that fails first.
        """
        proc, _ = self._run(wait_secs=2, home_exists=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hermes dashboard", proc.stdout)

    def test_a_start_outside_the_operator_does_not_wait_at_all(self):
        """No HERMES_MANAGED_DIR means no second container, so nobody is coming.

        compose, a plain manifest, `docker run`, the kustomize bases, a test harness: a
        missing config.yaml there is a fact, not a race, and pausing on it turns a fast
        failure into a two-minute one for no possible gain. This is not hypothetical —
        it is how the first cut of this wait hung deploy/docker's startup-contract tests,
        which run the entrypoint as `sh -c pwd` against an empty temp home.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "data"
            home.mkdir(parents=True)
            proc = subprocess.run(
                ["sh", str(_ENTRYPOINT), "echo", "hermes", "dashboard"],
                capture_output=True,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "PLATFORM_AGENT_HOME": str(home),
                    "AGENT_SHARED_STATE_SETUP": "skip",
                    # Deliberately no HERMES_MANAGED_DIR. The budget is long enough that
                    # waiting at all would blow the timeout below.
                    "AGENT_SHARED_STATE_WAIT_SECS": "600",
                },
                timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("waiting up to", proc.stderr)
        self.assertIn("hermes dashboard", proc.stdout)


def _extract_heredoc(marker):
    """Return the body of the entrypoint's `<<'MARKER'` heredoc.

    Step 2d's back-fill is a Python program embedded in the shell script, so the only
    way to test the program that actually ships is to lift it back out. Copying it into
    this file instead would be worse than no test: the copy would keep passing after the
    original diverged from it.

    Insists on exactly one opener. A second heredoc with the same marker would make this
    silently test whichever came first.
    """
    lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    openers = [i for i, line in enumerate(lines) if f"<<'{marker}'" in line]
    if len(openers) != 1:
        raise AssertionError(f"expected one <<'{marker}' in {_ENTRYPOINT}, found {len(openers)}")
    start = openers[0] + 1
    for end in range(start, len(lines)):
        if lines[end] == marker:
            return "\n".join(lines[start:end]) + "\n"
    raise AssertionError(f"<<'{marker}' is never closed in {_ENTRYPOINT}")


def _extract_shell_function(name):
    """Return the text of a `name() { ... }` function from the entrypoint.

    Same bargain as `_extract_heredoc`: run the shipped definition, never a copy of it.
    Relies on the script's own formatting — opener line, body, a `}` in column zero —
    which `sh -n` and the repo's shell style already enforce.
    """
    lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    openers = [i for i, line in enumerate(lines) if line.startswith(f"{name}() {{")]
    if len(openers) != 1:
        raise AssertionError(f"expected one {name}() in {_ENTRYPOINT}, found {len(openers)}")
    start = openers[0]
    for end in range(start + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start : end + 1]) + "\n"
    raise AssertionError(f"{name}() is never closed in {_ENTRYPOINT}")


def _extract_shell_block(opener, closer="fi"):
    """Return the text of a top-level block, from the line equal to `opener` to `closer`.

    Same bargain as `_extract_shell_function`, for the steps that are written inline
    rather than as a function: run the shipped lines, never a paraphrase of them. The
    block must start at column zero and end at the first later line equal to `closer`,
    which is how the script already writes its top-level steps.
    """
    lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line == opener]
    if len(starts) != 1:
        raise AssertionError(f"expected one `{opener}` in {_ENTRYPOINT}, found {len(starts)}")
    start = starts[0]
    for end in range(start + 1, len(lines)):
        if lines[end] == closer:
            return "\n".join(lines[start : end + 1]) + "\n"
    raise AssertionError(f"`{opener}` is never closed by `{closer}` in {_ENTRYPOINT}")


class FreshVolumeDetectionTest(unittest.TestCase):
    """A fresh volume is not an absent file, and getting that wrong cost a whole install.

    Upstream's stage2 hook seeds $HERMES_HOME/config.yaml from cli-config.yaml.example
    before this script runs, so `[ ! -f config.yaml ]` never fires and step 2d took the
    FILL-ONLY path into upstream's example. Fill-only cannot overrule a key that is
    present, so a new install kept upstream's terminal, browser, code_execution,
    delegation and telemetry defaults permanently — 26 top-level keys of example where
    the template asks for 9. Measured on a live cluster, not deduced.

    So the trigger is the example itself, byte-for-byte. These tests pin both halves:
    that a pristine example is recognised, and that anything the agent has touched is
    not — the second being the one that would turn this into the config-destroying
    force-copy the whole PR exists to avoid.
    """

    _FUNC = None

    @classmethod
    def setUpClass(cls):
        cls._FUNC = _extract_shell_function("config_is_pristine_upstream_example")

    def _is_pristine(self, live, example):
        """Run the shipped function over two real files; True iff it exits 0."""
        with tempfile.TemporaryDirectory() as tmp:
            live_path = pathlib.Path(tmp) / "config.yaml"
            example_path = pathlib.Path(tmp) / "cli-config.yaml.example"
            if live is not None:
                live_path.write_text(live, encoding="utf-8")
            if example is not None:
                example_path.write_text(example, encoding="utf-8")
            proc = subprocess.run(
                ["sh", "-c", self._FUNC + f'\nconfig_is_pristine_upstream_example "{live_path}" "{example_path}"'],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return proc.returncode == 0

    def test_the_untouched_example_is_recognised(self):
        """What stage2 leaves on a brand-new PVC: a verbatim copy."""
        example = "model:\n  provider: anthropic\nterminal:\n  enabled: true\n"
        self.assertTrue(self._is_pristine(example, example))

    def test_a_config_the_agent_has_written_is_not(self):
        """One `/sethome` is enough to end the equality, and must be."""
        example = "model:\n  provider: anthropic\nterminal:\n  enabled: true\n"
        live = "model: {}\nterminal:\n  enabled: true\nplatforms:\n  slack:\n    home_channel: C123\n"
        self.assertFalse(self._is_pristine(live, example))

    def test_a_one_byte_difference_is_enough(self):
        """A byte compare, not a fuzzy one: near-miss must fall through to the back-fill."""
        example = "model:\n  provider: anthropic\n"
        self.assertFalse(self._is_pristine(example + "\n", example))

    def test_a_missing_example_is_not_pristine(self):
        """An image without the example must take the back-fill path, not overwrite.

        This is the forward-compatibility case: if upstream moves or renames the file,
        the answer has to be "leave the live config alone", never "replace it".
        """
        self.assertFalse(self._is_pristine("model: {}\n", None))

    def test_a_missing_live_config_is_not_pristine(self):
        """That case belongs to the seed branch above it, which needs no comparison."""
        self.assertFalse(self._is_pristine(None, "model: {}\n"))


class ConfigBackfillTest(unittest.TestCase):
    """Step 2d fills keys the live config.yaml has lost, and must change nothing else.

    Two things hollow the PVC file out, and both are ordinary: hermes' `save_config`
    strips every leaf the managed scope holds before writing, so one `/sethome` leaves
    `model: {}` on disk; and a release that stops pinning a leaf hands it back to a file
    the previous release already emptied of it. The pod then runs on hermes' built-in
    defaults with green health checks — the failure this step exists to prevent.

    The back-fill is the only thing in the entrypoint that rewrites a file the running
    agent owns, so both halves matter: what it restores, and what it refuses to touch.
    Overruling one value the agent wrote for itself would make it the three-way merge
    this PR deleted, whose rule kept a bad value alive across every restart (#658).
    """

    _PROGRAM = None

    @classmethod
    def setUpClass(cls):
        cls._PROGRAM = _extract_heredoc("PYEOF")

    def _fill(self, template, live):
        """Run the real program over two files, returning `(proc, reloaded_live)`."""
        with tempfile.TemporaryDirectory() as tmp:
            template_path = pathlib.Path(tmp) / "template.yaml"
            live_path = pathlib.Path(tmp) / "live.yaml"
            template_path.write_text(
                template if isinstance(template, str) else yaml.safe_dump(template),
                encoding="utf-8",
            )
            live_path.write_text(
                live if isinstance(live, str) else yaml.safe_dump(live), encoding="utf-8"
            )
            before = live_path.read_bytes()
            proc = subprocess.run(
                [sys.executable, "-c", self._PROGRAM, str(template_path), str(live_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            after = live_path.read_bytes()
            return proc, yaml.safe_load(after), before == after

    def test_a_key_the_live_file_lost_is_restored(self):
        """`model: {}` is what a save leaves behind; the template's block goes back in."""
        proc, live, _ = self._fill(
            {"model": {"base_url": "http://litellm/v1", "api_mode": "chat_completions"}},
            {"model": {}},
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(live["model"]["base_url"], "http://litellm/v1")
        self.assertEqual(live["model"]["api_mode"], "chat_completions")
        self.assertIn("model.base_url", proc.stdout)

    def test_a_value_the_agent_wrote_is_never_overruled(self):
        """Including one deliberately set empty — absence is the only trigger."""
        proc, live, _ = self._fill(
            {"platforms": {"slack": {"home_channel": "C-TEMPLATE", "rich_blocks": True}}},
            {"platforms": {"slack": {"home_channel": "", "rich_blocks": False}}},
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            live["platforms"]["slack"]["home_channel"],
            "",
            "an empty home channel is a value the agent set, not a missing key: "
            "restoring the template's over it is the /sethome bug of #658",
        )
        self.assertIs(live["platforms"]["slack"]["rich_blocks"], False)

    def test_a_live_scalar_is_not_descended_into(self):
        """Where the live file holds a scalar and the template a mapping, the agent wins."""
        proc, live, _ = self._fill({"memory": {"provider": "hindsight"}}, {"memory": "none"})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(live["memory"], "none")

    def test_nothing_missing_leaves_the_file_byte_identical(self):
        """No rewrite when there is nothing to add — that is what keeps the comments."""
        live = "# the agent's own file\nmodel:\n  base_url: http://litellm/v1\n"
        proc, _, unchanged = self._fill({"model": {"base_url": "http://other/v1"}}, live)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(unchanged, f"the file was rewritten with nothing to add: {proc.stdout}")

    def test_a_live_file_that_is_not_a_mapping_is_skipped(self):
        """A corrupt config must not take the container down; step 2d only reports."""
        proc, live, unchanged = self._fill({"model": {"base_url": "http://litellm/v1"}}, "- a\n- b")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(unchanged)
        self.assertEqual(live, ["a", "b"])
        self.assertIn("skipped", proc.stderr)

    def test_the_shipped_template_repairs_a_hollowed_config(self):
        """The real files, in the shape the cluster produced: `model: {}` plus /sethome."""
        template = yaml.safe_load(_CHAT_TEMPLATE.read_text(encoding="utf-8"))
        proc, live, _ = self._fill(
            template,
            {
                "model": {},
                "platforms": {"google_chat": {"home_channel": "spaces/AAQA"}},
                "monitoring": {"install_id": "abc123"},
            },
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(live["model"], template["model"])
        self.assertEqual(
            live["platforms"]["google_chat"]["home_channel"],
            "spaces/AAQA",
            "the back-fill overwrote the home channel /sethome set — the one thing #658 "
            "is about keeping",
        )
        self.assertEqual(live["monitoring"]["install_id"], "abc123")
        for key in ("toolsets", "platform_toolsets", "kanban", "agent"):
            self.assertIn(key, live, f"{key} was lost to the managed strip and not restored")


class ManagedScopeAssertionTest(unittest.TestCase):
    """Step 2d's other half: the check that the operator's pins actually arrived.

    Hermes' managed scope fails OPEN — an absent or unparseable file is ignored — which
    for us means the model endpoint is silently writable on a pod whose health checks
    stay green. The entrypoint names the leaves it expects rather than counting them,
    and a typo in one of those names makes the check pass vacuously. Nothing else in
    this repository compares that list to what the operator emits: one side is shell,
    the other Go.
    """

    def test_the_expected_keys_are_keys_the_operator_actually_pins(self):
        script = _ENTRYPOINT.read_text(encoding="utf-8")
        match = re.search(r"for expected in \(([^)]*)\):", script)
        self.assertIsNotNone(
            match, "step 2d no longer names the leaves it expects; this test guards that list"
        )
        expected = ast.literal_eval(f"({match.group(1)})")
        self.assertTrue(expected, "an empty expectation would make the check pass vacuously")

        managed = None
        for doc in yaml.safe_load_all(_MANIFEST_GOLDEN.read_text(encoding="utf-8")):
            if doc and doc.get("kind") == "ConfigMap" and "managed-config.yaml" in (
                doc.get("data") or {}
            ):
                managed = yaml.safe_load(doc["data"]["managed-config.yaml"])
        self.assertIsNotNone(managed, f"no managed-config.yaml in {_MANIFEST_GOLDEN}")

        for dotted in expected:
            node = managed
            for part in dotted.split("."):
                self.assertIsInstance(
                    node, dict, f"step 2d expects {dotted}, which the render does not nest that way"
                )
                self.assertIn(
                    part,
                    node,
                    f"step 2d warns unless the managed scope pins {dotted}, but the operator "
                    f"does not render it — every boot would log 'running UNPINNED'",
                )
                node = node[part]


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


class PlatformFrontDoorTest(unittest.TestCase):
    """The startup decisions that turn on spec.harness.experimental.platformFrontDoor.

    The operator renders that flag as HERMES_GATEWAY_PROFILE=platform on the gateway
    container, and the gateway then runs as the platform profile instead of the default
    one. That moves profiles/platform/config.yaml out of the image's ownership and into
    the agent's: `/sethome` persists the home channel into it and the monitoring policy
    mints monitoring.install_id there. Step 2.6 must therefore stop force-syncing it, and
    step 2.6b must back-fill it instead — the same bargain step 2d strikes for the default
    profile's own file.

    Both ask the same predicate, so it is tested once and each caller is tested against
    it — the failure worth catching is the two disagreeing, which is either a config.yaml
    force-synced out from under the agent or one that never takes a key the image added.

    Extracted rather than run through the whole script for the reason step 2.6a's tests
    give: the surrounding steps are guarded on /opt/hermes and /opt/agent-config, which
    exist only inside the image.
    """

    def _run(self, snippet, profile=None):
        """Run `snippet` under `set -e` with the front-door helpers in scope."""
        script = "set -e\n"
        for name in ("platform_is_front_door", "platform_sync_items"):
            script += _extract_shell_function(name) + "\n"
        script += snippet
        env = {"PATH": "/usr/bin:/bin"}
        if profile is not None:
            env["HERMES_GATEWAY_PROFILE"] = profile
        return subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, timeout=60, env=env
        )

    def _predicate(self, snippet, profile=None):
        proc = self._run(f'if {snippet}; then echo YES; else echo NO; fi\n', profile=profile)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip() == "YES"

    def _items(self, profile=None):
        proc = self._run("platform_sync_items\n", profile=profile)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split()

    def test_the_front_door_is_off_unless_the_operator_names_this_profile(self):
        """Only the exact value opts in; everything else is the behaviour every install has.

        The empty string matters on its own: Kubernetes renders an absent value as one
        rather than omitting the variable, so a half-wired manifest arrives here as `""`.
        """
        for profile in (None, "", "  ", "default", "cluster-prod", "Platform", "platform2"):
            self.assertFalse(
                self._predicate("platform_is_front_door", profile=profile),
                f"HERMES_GATEWAY_PROFILE={profile!r} must not opt an install in",
            )
        self.assertTrue(self._predicate("platform_is_front_door", profile="platform"))

    def test_the_front_door_drops_config_yaml_and_nothing_else(self):
        """config.yaml is the only entry whose ownership the flag changes.

        Asserted as a set difference rather than against a spelled-out list, because the
        bug to catch is a persona or a directory quietly falling out of the sync on
        front-door installs only — where it would present months later as an agent whose
        skills never updated, on the one install nobody compares against the image.
        """
        default = self._items()
        front_door = self._items(profile="platform")

        self.assertIn("config.yaml", default)
        self.assertEqual(
            set(default) - set(front_door),
            {"config.yaml"},
            "the front door must drop config.yaml from the force-sync and keep the rest",
        )
        self.assertEqual(
            set(front_door) - set(default), set(), "the front door must not add entries"
        )

    def test_step_2_6b_fills_the_platform_config_with_step_2ds_own_program(self):
        """One program, two callers — asserted on the source, not on a copy of it.

        The rule step 2.6b needs is exactly step 2d's: restore a key the image declares
        and the live file has lost, never overrule one the agent wrote. Re-implementing
        it here is the failure this catches, because the second copy would drift towards
        the three-way merge that #658 removed — and the two files it governs are the two
        an agent writes to, so a divergence surfaces as `/sethome` sticking on one
        profile and not the other.
        """
        source = _ENTRYPOINT.read_text(encoding="utf-8")
        callers = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith("backfill_config_from_template")
            and not line.strip().endswith("() {")
        ]
        self.assertEqual(
            len(callers),
            2,
            f"expected step 2d and step 2.6b to be the only callers, found {callers}",
        )
        self.assertIn(
            "$PLATFORM_TEMPLATE/config.yaml",
            source,
            "step 2.6b must fill from the platform profile's image template",
        )

    def test_step_2_6b_runs_only_at_the_front_door_and_only_on_the_primary(self):
        """The guard, read off the source, because both halves are silent when wrong.

        Without the predicate the fill runs on every install, on a file step 2.6 has
        already force-synced — harmless the first time and wrong the moment the template
        and the overlay disagree. Without the primary check a second container races the
        first over one file on a shared PVC, which is what step 1.5 exists to stop.
        """
        lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        call = next(
            i
            for i, line in enumerate(lines)
            if line.strip().startswith("backfill_config_from_template")
            and "PLATFORM_TEMPLATE" in "\n".join(lines[i : i + 3])
        )
        guard = next(lines[i] for i in range(call, -1, -1) if lines[i].startswith("if "))
        self.assertIn("platform_is_front_door", guard)
        self.assertIn("IS_BOOTSTRAP_PRIMARY", guard)

    def test_step_2_6_asks_the_helper_rather_than_carrying_its_own_list(self):
        """The call site, not just the helper — a literal here re-opens the whole bug.

        Everything above tests platform_sync_items in isolation, which stays green if
        step 2.6's `--items` is edited back to the spelled-out list it used to carry:
        the helper would go on answering correctly and nothing would ask it. The result
        is config.yaml force-synced over the front door's own file on every restart,
        discarding `/sethome` and monitoring.install_id, silently.
        """
        lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        # Two invocations name this profile — step 2.5 scaffolds it when absent, step 2.6
        # force-syncs it on every boot — and only the second passes --items at all.
        items = []
        for i, line in enumerate(lines):
            if line.strip() != "--name platform \\":
                continue
            items += [a.strip() for a in lines[i : i + 8] if a.strip().startswith("--items")]
        self.assertEqual(
            items,
            ['--items "$(platform_sync_items)" \\'],
            "step 2.6 must resolve its --items through platform_sync_items, not a literal",
        )

    def _run_step_2_6b(self, template, live):
        """Run the shipped step 2.6b block over a real profile tree.

        Lifted out by its guard rather than copied, for `_extract_heredoc`'s reason: a
        copy would keep passing after the block it stands in for changed. Everything the
        block reads is a variable, so a temp tree and an env is the whole fixture — the
        one exception being `$INSTALL_DIR/.venv/bin/python3`, which the fill arm calls
        and which is faked here with a wrapper script executing the interpreter running the tests.

        `template` and `live` are YAML text; `live=None` means the file is absent, which
        is the branch under test. Returns (CompletedProcess, live text or None).
        """
        lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        starts = [i for i, line in enumerate(lines) if line.startswith("if platform_is_front_door ")]
        self.assertEqual(len(starts), 1, "expected exactly one step 2.6b block")
        end = next(i for i in range(starts[0], len(lines)) if lines[i] == "fi")
        block = "\n".join(lines[starts[0] : end + 1])

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "template").mkdir()
            (root / "template" / "config.yaml").write_text(template, encoding="utf-8")
            profile = root / "data" / "profiles" / "platform"
            profile.mkdir(parents=True)
            if live is not None:
                (profile / "config.yaml").write_text(live, encoding="utf-8")
            venv = root / "install" / ".venv" / "bin"
            venv.mkdir(parents=True)
            wrapper = venv / "python3"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)

            script = "set -e\n" + _extract_shell_function("platform_is_front_door") + "\n"
            script += _extract_shell_function("backfill_config_from_template") + "\n" + block
            proc = subprocess.run(
                ["sh", "-c", script],
                capture_output=True,
                text=True,
                timeout=60,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HERMES_GATEWAY_PROFILE": "platform",
                    "IS_BOOTSTRAP_PRIMARY": "1",
                    "PLATFORM_TEMPLATE": str(root / "template"),
                    "TARGET_DIR": str(root / "data"),
                    "INSTALL_DIR": str(root / "install"),
                },
            )
            written = (profile / "config.yaml").read_text(encoding="utf-8") if (profile / "config.yaml").exists() else None
            return proc, written

    def test_step_2_6b_seeds_the_config_when_the_profile_has_none(self):
        """The absent-file arm, which is the only thing left that can recreate this file.

        With config.yaml off the force-sync, the four steps that could write it are step
        2.5 (gated on the profile being ABSENT, so not once profile.yaml exists), step
        2.6 (no longer carries the name in --items), this block, and step 2.7 (skips a
        profile whose config.yaml is missing). If this arm does not seed, a profile that
        registered without a config — profile_scaffold writes profile.yaml before it
        copies the template, and step 2.5's caller swallows a failure between the two
        with a WARN — never gets one back for the life of the volume. It is also the
        profile receiving chat, and an absent `platform_toolsets` does not fail closed:
        `resolve_toolset` auto-generates the full core bundle plus every enabled MCP
        server, with no `agent.disabled_toolsets` ceiling and no operator overlay.
        """
        template = "platform_toolsets:\n  google_chat: [kanban]\nmonitoring: {}\n"
        proc, written = self._run_step_2_6b(template, live=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(written, template, "an absent config must be seeded from the image template")

    def test_step_2_6b_fills_an_existing_config_without_overruling_it(self):
        """The other arm, on the same fixture: fill what is missing, keep what is there.

        Pinned next to the seed so the two cannot be confused for each other. Seeding
        over a live file would discard `/sethome` and monitoring.install_id on every
        restart, which is the failure taking config.yaml off the force-sync exists to
        prevent.
        """
        template = "platform_toolsets:\n  google_chat: [kanban]\nmonitoring: {}\n"
        live = "platform_toolsets:\n  google_chat: [kanban, memory]\n"
        proc, written = self._run_step_2_6b(template, live=live)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        parsed = yaml.safe_load(written)
        self.assertEqual(
            parsed["platform_toolsets"]["google_chat"],
            ["kanban", "memory"],
            "the fill must not overrule a key the agent already wrote",
        )
        self.assertIn("monitoring", parsed, "the fill must add a key the template declares")

    def test_the_predicate_survives_set_e_when_it_is_false(self):
        """The off path is every existing install, so a false return must not end the boot.

        `platform_is_front_door || _items=...` and `if platform_is_front_door` are both
        exempt from `set -e`, but a future caller written as a bare command would not be,
        and the symptom is a CrashLoopBackOff on the installs that did NOT opt in.
        """
        proc = self._run("platform_sync_items >/dev/null\necho SURVIVED\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SURVIVED", proc.stdout)

    def test_step_2c_invalidates_local_mcp_schema_cache(self):
        """Step 2c drops local MCP server cache entries on startup so new tools appear."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            defaults = root / "defaults" / "scripts"
            defaults.mkdir(parents=True)
            invalidate_src = pathlib.Path(__file__).resolve().parent.parent / "agents" / "platform" / "scripts" / "invalidate_mcp_cache.py"
            shutil.copy(invalidate_src, defaults / "invalidate_mcp_cache.py")

            data = root / "data"
            (data / "scripts").mkdir(parents=True)
            shutil.copy(invalidate_src, data / "scripts" / "invalidate_mcp_cache.py")
            cache_dir = data / "profiles" / "platform" / "cache"
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / "mcp_schema_cache.json"
            cache_file.write_text(
                json.dumps({
                    "platform_control": {"fingerprint": "abc", "tools": [{"name": "verify_gke_cluster"}]},
                    "developer_knowledge": {"fingerprint": "def", "tools": [{"name": "search_docs"}]},
                }),
                encoding="utf-8",
            )

            lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
            start = next(i for i, line in enumerate(lines) if line.startswith('# 2c. Invalidate locally-hosted MCP server schemas'))
            end = next(i for i in range(start, len(lines)) if lines[i] == "fi")
            block = "\n".join(lines[start : end + 1])

            proc = subprocess.run(
                ["sh", "-c", "set -e\n" + block],
                capture_output=True,
                text=True,
                timeout=60,
                env={
                    "PATH": "/usr/bin:/bin",
                    "TARGET_DIR": str(data),
                    "PYTHONPATH": str(defaults),
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            updated = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertNotIn("platform_control", updated)
            self.assertIn("developer_knowledge", updated)


class ApiKeyPinCheckTest(unittest.TestCase):
    """The startup self-check for issue #786.

    The bug it names: Hermes' stage2 hook generates a random API_SERVER_KEY into the PVC
    `.env` when that file has none, `load_hermes_dotenv` applies that file over the
    container environment, and so the gateway authenticated against a value invented at
    boot that nothing else in the pod knew — every authenticated call 401'd, including
    the container's own startup probe. The operator now pins the key in the managed
    `.env`, which is applied last; this check is what says so out loud when the pin is
    missing, because managed scope fails open and the probe alone reports only THAT
    something is wrong.

    What these hold is the shape of the report, not its wording: warns when the file is
    absent, warns when it pins a different value, is silent when the two agree, and never
    ends the boot — a pod that comes up unpinned still answers, and failing the container
    would trade a degraded API for none.
    """

    @classmethod
    def setUpClass(cls):
        cls._FUNC = _extract_shell_function("warn_unless_api_key_is_pinned")

    def _run(self, managed_env, key="cluster-internal-trusted"):
        """Run the shipped function over a real file; returns the completed process.

        `managed_env` is the file's content, or None for "the mount did not arrive".
        `set -e` is on, as it is in the entrypoint, so a check that failed the boot on
        the warning paths would show up here as a non-zero exit.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / ".env"
            if managed_env is not None:
                path.write_text(managed_env, encoding="utf-8")
            return subprocess.run(
                ["sh", "-c", f'set -e\n{self._FUNC}\nwarn_unless_api_key_is_pinned "{path}" "{key}"\necho SURVIVED\n'],
                capture_output=True,
                text=True,
                timeout=60,
            )

    def test_an_agreeing_pin_says_nothing(self):
        """The steady state is every boot, so it must not warn."""
        proc = self._run("API_SERVER_KEY=cluster-internal-trusted\nSLACK_ALLOW_ALL_USERS=false\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

    def test_a_missing_managed_env_is_named(self):
        proc = self._run(None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("786", proc.stderr)
        self.assertIn("NOT pinned", proc.stderr)

    def test_a_pin_that_disagrees_is_named(self):
        """A stale ConfigMap pinning a key nobody presents.

        Distinct from "no pin at all" in the one way an operator acts on: managed scope is
        applied last, so HERE the managed file is the winner and the container's env is
        what loses. Told the PVC won, they would go and read the wrong file.
        """
        proc = self._run("API_SERVER_KEY=" + "a1b2" * 16 + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("786", proc.stderr)
        self.assertIn("to a different value", proc.stderr)
        self.assertNotIn(".env wins", proc.stderr)

    def test_a_managed_env_that_pins_other_keys_is_a_disagreement(self):
        """A chat-only render is exactly the file this shipped with before the fix.

        Nothing overrides the key, so the winner is the PVC's stage2-generated one — the
        opposite file from the branch above, and the message has to say so.
        """
        proc = self._run("GOOGLE_CHAT_ALLOW_ALL_USERS=false\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("does not pin API_SERVER_KEY", proc.stderr)
        self.assertIn(".env wins", proc.stderr)

    def test_a_line_that_merely_contains_the_value_is_not_the_pin(self):
        """`grep -x`, not a substring: a different key holding the same value agrees
        about nothing, and neither does a commented-out pin."""
        proc = self._run("# API_SERVER_KEY=cluster-internal-trusted\nOTHER=cluster-internal-trusted\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("does not pin API_SERVER_KEY", proc.stderr)

    def test_it_never_ends_the_boot(self):
        """Report, never exit — asserted on the warning paths, where it would matter."""
        for content in (None, "API_SERVER_KEY=something-else\n"):
            with self.subTest(managed_env=content):
                proc = self._run(content)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("SURVIVED", proc.stdout)


class SandboxMirrorGateTest(unittest.TestCase):
    """Step 5.7 pushes the profile layout into the shell sandbox, and is fatal.

    The agent's Kubernetes credentials are read-only, so nothing in this container can
    write an Event or a condition to say the migration failed. Exiting non-zero is the
    one channel it has: the kubelet restarts the container, and the operator's
    getDeploymentStatusDetails scans the gateway pod's container statuses and copies the
    waiting reason into the CR as phase Degraded. Come up 0 instead and the CR reads
    Ready while the model's files sit on this volume where its shell cannot see them —
    which is what happened on a live upgrade, to ten cluster profile homes at once.

    So the exit status is the contract, and these pin both directions of it. The
    transient case does not arrive here as a failure at all: sandbox_mirror.py returns 0
    when the sandbox has not started yet and leaves its marker unwritten, so the next
    start retries. What reaches this block non-zero is a wrong --remote-root or a
    transfer that ran and failed, and neither gets better on its own.
    """

    @classmethod
    def setUpClass(cls):
        cls._BLOCK = _extract_shell_block(
            'SANDBOX_MIRROR_SCRIPT="/opt/defaults/scripts/sandbox_mirror.py"'
        )

    def _run(self, rc=0, primary="1", install_script=True):
        """Run the shipped block with a python3 that exits `rc`.

        Returns `(proc, invoked)` — `invoked` says whether the interpreter was reached
        at all, which is the only way to tell "the mirror passed" from "the gate above
        it never ran".
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            target = tmp / "data"
            (target / "logs").mkdir(parents=True)
            (target / "scripts").mkdir()
            if install_script:
                (target / "scripts" / "sandbox_mirror.py").write_text("", encoding="utf-8")

            # Stands in for the venv interpreter, and records that it ran. The log line
            # it writes is what the failure path is supposed to put in front of an
            # operator, so the test can assert the tail carried it out to stderr.
            marker = tmp / "invoked"
            python = tmp / "hermes" / ".venv" / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text(
                "#!/bin/sh\n"
                f'echo "$@" >"{marker}"\n'
                'echo "mirror said something an operator needs to read"\n'
                f"exit {rc}\n",
                encoding="utf-8",
            )
            python.chmod(0o755)

            proc = subprocess.run(
                ["sh", "-c", f"set -e\n{self._BLOCK}\necho REACHED-EXEC\n"],
                capture_output=True,
                text=True,
                timeout=60,
                env={
                    **os.environ,
                    "TARGET_DIR": str(target),
                    "INSTALL_DIR": str(tmp / "hermes"),
                    "IS_BOOTSTRAP_PRIMARY": primary,
                },
            )
            return proc, marker.exists()

    def test_a_failed_migration_ends_start_up(self):
        proc, invoked = self._run(rc=1)
        self.assertTrue(invoked, "the mirror never ran, so this asserts nothing")
        self.assertNotEqual(
            proc.returncode,
            0,
            "a migration that failed left the container coming up healthy; the CR will "
            "say Ready and nothing will say the model's files did not cross",
        )
        self.assertNotIn("REACHED-EXEC", proc.stdout)

    def test_the_failure_names_itself_and_carries_the_log_out(self):
        """`kubectl logs` is where the CR's Degraded message sends you; it has to say why."""
        proc, _ = self._run(rc=1)
        self.assertIn("FATAL", proc.stderr)
        self.assertIn(
            "mirror said something an operator needs to read",
            proc.stderr,
            "the cause stayed in logs/sandbox_mirror.log on the PVC, which is exactly "
            "the silence this change exists to end",
        )

    def test_a_clean_migration_leaves_start_up_alone(self):
        proc, invoked = self._run(rc=0)
        self.assertTrue(invoked)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REACHED-EXEC", proc.stdout)

    def test_a_non_primary_replica_does_not_run_it(self):
        """Two tar streams into one directory race; only the primary mirrors.

        Run with a mirror that would fail, so a regression that drops the gate fails
        this loudly rather than passing on a stub that happens to succeed.
        """
        proc, invoked = self._run(rc=1, primary="0")
        self.assertFalse(invoked)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REACHED-EXEC", proc.stdout)

    def test_an_image_without_the_script_is_not_a_failure(self):
        """The block is guarded on the script existing, and predates it in some images."""
        proc, invoked = self._run(rc=1, install_script=False)
        self.assertFalse(invoked)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REACHED-EXEC", proc.stdout)


if __name__ == "__main__":
    unittest.main()
