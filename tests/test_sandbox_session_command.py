"""Tests for the profile-home hop in deploy/sandbox/session-command.sh.

    python3 -m unittest discover -s tests -p 'test_*.py'

The script is sshd's ForceCommand for the sandbox's `agent` account. Here it runs
on the host under a temporary data root, with SSH_ORIGINAL_COMMAND in the exact
shape Hermes' ssh backend sends and HERMES_PROFILE_HOME set the way the agent
image's ssh client sets it. Everything it decides is visible in the environment
of the command it finally execs, so each case runs a probe that prints the
variables and asserts on that line. Section 4e of deploy/sandbox/smoke-test.sh
runs the same decisions over a real sshd; this file is what runs on every pull
request without Docker.

What is being pinned: the client's word is read as a profile *name* and rebased
onto this root; it wins over the working directory; a name that is not one path
component, or that names no home here, changes nothing and says why on stderr;
and nothing evaluates the value.
"""

import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "deploy" / "sandbox" / "session-command.sh"

# The command every case runs on the far side of the pretend crossing. Brackets
# so that an empty value reads as empty rather than as a missing field.
_PROBE = (
    'echo "home=[$HERMES_HOME] kc=[$KUBECONFIG]'
    ' ws=[$HERMES_KANBAN_WORKSPACE] task=[$HERMES_KANBAN_TASK]"'
)

# A shared-root scratch workspace, the shape the dispatcher gives every card on
# the default board and the one the cwd derivation cannot place.
_TASK_ID = "t_00ae853b"


def _wire(cwd: str, command: str) -> str:
    """`bash -c <shlex.quote(script)>` around base.py's command wrapper: what
    tools/environments/ssh.py puts on the wire, and the only shape the script
    unwraps."""
    script = (
        f"builtin cd -- {shlex.quote(cwd)} || exit 126\n"
        f"eval {shlex.quote(command)}\n"
        "__hermes_ec=$?\n"
        "exit $__hermes_ec"
    )
    return f"bash -c {shlex.quote(script)}"


class SessionCommandProfileHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.profiles = self.root / "profiles"
        self.mirrored = self.profiles / "cluster-a"
        self.mirrored.mkdir(parents=True)
        (self.mirrored / "kubeconfig.yaml").write_text("kubeconfig\n")
        (self.profiles / "platform").mkdir()
        self.workspace = self.root / "kanban" / "workspaces" / _TASK_ID

    def _run(self, cwd: str, named: str | None = None,
             command: str = _PROBE, wrapped: bool = True) -> subprocess.CompletedProcess:
        # A minimal environment, built rather than inherited: the host's own
        # KUBECONFIG or HERMES_KANBAN_* would otherwise read as the script's
        # doing. HERMES_HOME is the root, as the sandbox's sshd sets it.
        env = {
            "PATH": os.environ["PATH"],
            "HERMES_HOME": str(self.root),
            "SSH_ORIGINAL_COMMAND": _wire(cwd, command) if wrapped else command,
        }
        if named is not None:
            env["HERMES_PROFILE_HOME"] = named
        return subprocess.run(
            ["bash", str(_SCRIPT)],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_the_clients_name_beats_a_shared_root_workspace(self) -> None:
        """The gap the forwarded name closes: a card's workspace is under no
        profile home, and the name under a different agent-pod root is rebased
        onto this one."""
        proc = self._run(str(self.workspace), named="/mnt/agent-data/profiles/cluster-a")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            f"home=[{self.mirrored}] kc=[{self.mirrored}/kubeconfig.yaml]"
            f" ws=[{self.workspace}] task=[{_TASK_ID}]",
        )
        self.assertEqual(proc.stderr, "", "a good name is not remarked on")

    def test_without_the_name_a_shared_root_workspace_keeps_the_root(self) -> None:
        """The fallback, and the failure the name exists to fix: with nothing
        from the client the cwd says nothing and the root stays."""
        proc = self._run(str(self.workspace))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"home=[{self.root}] kc=[]", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_the_name_beats_a_cwd_under_another_profile(self) -> None:
        """A worker's HERMES_HOME is who it is; its cwd is only where it works."""
        other = self.profiles / "platform" / "kanban" / "workspaces" / "t_ac37a4c2"
        proc = self._run(str(other), named=f"{self.root}/profiles/cluster-a")
        self.assertIn(f"home=[{self.mirrored}]", proc.stdout)
        self.assertIn(f"ws=[{other}]", proc.stdout, "the kanban derivation still runs")

    def test_the_root_itself_names_no_profile_and_says_nothing(self) -> None:
        """A worker on the default profile sends the root, and wants it."""
        proc = self._run(str(self.workspace), named=str(self.root))
        self.assertIn(f"home=[{self.root}] kc=[]", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_the_cwd_still_narrows_when_the_name_is_the_root(self) -> None:
        """The name only takes precedence when it names a profile; a command
        run from inside a profile home with the root forwarded is that
        profile's, as before."""
        proc = self._run(str(self.mirrored / "work"), named=str(self.root))
        self.assertIn(f"home=[{self.mirrored}]", proc.stdout)

    def test_a_name_that_is_not_one_component_is_refused(self) -> None:
        """The client picks among the homes this volume has, and nothing else:
        `..`, a slash, an empty component and `.` all leave the root."""
        for value in (
            f"{self.root}/profiles/..",
            f"{self.root}/profiles/../../etc",
            f"{self.root}/profiles/cluster-a/",
            f"{self.root}/profiles/",
            f"{self.root}/profiles/.",
        ):
            with self.subTest(value=value):
                proc = self._run(str(self.workspace), named=value)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"home=[{self.root}] kc=[]", proc.stdout)
                self.assertIn("is not a profile", proc.stderr)
                self.assertIn(value, proc.stderr, "the refusal names what it refused")

    def test_an_unmirrored_profile_falls_back_and_says_so(self) -> None:
        """A profile the agent pod has and this volume does not yet: the cwd
        decides, and the message says which profile the sandbox is missing so
        the preflight's complaint has an explanation beside it."""
        proc = self._run(str(self.workspace), named=f"{self.root}/profiles/cluster-b")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f"home=[{self.root}] kc=[]", proc.stdout)
        self.assertIn("profile cluster-b is not mirrored into the sandbox yet", proc.stderr)
        self.assertIn(str(self.profiles / "cluster-b"), proc.stderr)

    def test_the_fallback_after_an_unmirrored_name_is_the_cwd(self) -> None:
        """Falling back means the cwd derivation runs, not that nothing does."""
        proc = self._run(str(self.mirrored / "work"), named=f"{self.root}/profiles/cluster-b")
        self.assertIn(f"home=[{self.mirrored}] kc=[{self.mirrored}/kubeconfig.yaml]", proc.stdout)
        self.assertIn("not mirrored", proc.stderr)

    def test_the_value_is_data(self) -> None:
        """A name carrying a space, quotes, a command substitution and a
        backtick is echoed back in the message and executed nowhere."""
        marker = "pwned"
        value = f"{self.root}/profiles/x y'\"$(touch {marker})`touch {marker}`"
        proc = self._run(str(self.workspace), named=value)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"home=[{self.root}]", proc.stdout, "the command still ran")
        self.assertFalse((self.root / marker).exists(), "the value was evaluated")
        self.assertIn("not mirrored into the sandbox yet", proc.stderr)
        self.assertIn(value, proc.stderr)

    def test_kubeconfig_only_when_the_profile_has_one(self) -> None:
        """Unset beats a path to a file that is not there: `kubectl` with a
        KUBECONFIG naming a missing file fails on every call."""
        proc = self._run(str(self.workspace), named=f"{self.root}/profiles/platform")
        self.assertIn(f"home=[{self.profiles / 'platform'}] kc=[]", proc.stdout)

    def test_a_command_that_is_not_a_hermes_wrapper_gets_it_too(self) -> None:
        """The profile is the session's, whatever the session runs: a plain
        command with no cd line is narrowed by the name alone."""
        proc = self._run("", named=f"{self.root}/profiles/cluster-a",
                         command='echo "home=[$HERMES_HOME] kc=[$KUBECONFIG]"', wrapped=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            f"home=[{self.mirrored}] kc=[{self.mirrored}/kubeconfig.yaml]",
        )


if __name__ == "__main__":
    unittest.main()
