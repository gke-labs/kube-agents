"""The umask that keeps the shared PVC writable from both containers.

The sandbox runs as UID 10000 and the credential sidecar as 10001 (see the UID
constants in the operator's `platformagent_manifests.go`). They share one PVC and
each writes files the other has to change: the sandbox creates the leased GitOps
directory the sidecar clones into, and the sidecar writes a kubeconfig pin into a
profile home the sandbox created. The kubelet's fsGroup pass makes files that
exist at mount time group-writable; for files created afterwards, the only thing
standing between the UID split and `EACCES` is that both sides run with
`umask 0002`.

That makes these three lines a control, and a control with no test is a control
that gets deleted. Deleting any one of them leaves every other suite in this
repository green and breaks the GitOps flow at runtime, in a way that shows up as
a skill failing to clone rather than as anything pointing back here.

There are three of them rather than two because a `replicas > 1` PlatformAgent
never runs `agent-entrypoint` at all: the operator gives the sandbox container a
`command`, which overrides the image ENTRYPOINT, and `leader_elect.py` starts
Hermes itself.

Run:
  python3 -m unittest discover -s tests -p 'test_startup_umask.py' -v
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

AGENT_ENTRYPOINT = REPO_ROOT / "deploy" / "shared" / "docker-entrypoint.sh"
CREDENTIAL_SIDECAR = REPO_ROOT / "deploy" / "shared" / "start-services.sh"
LEADER_ELECT = REPO_ROOT / "k8s-operator" / "internal" / "controller" / "leader_elect.py"

# Group write, world read. 0022 — the default the containers inherit without
# this — is what the assertions below exist to catch.
EXPECTED_UMASK = "0002"


def first_effective_command(script: Path) -> str:
    """The first line of `script` that can create a file.

    Shebang, blank lines, comments and shell-option lines cannot, so the umask is
    allowed to sit after those and nothing else. Checking the position rather
    than mere presence is deliberate: a `umask` below the first `mkdir` is a line
    that reads as the control while doing none of its job.
    """
    for line in script.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#!") or stripped.startswith("#"):
            continue
        if stripped.startswith("set -"):
            continue
        return stripped
    raise AssertionError(f"{script} has no commands in it at all")


class SharedWorkspaceUmaskTest(unittest.TestCase):
    def test_agent_entrypoint_sets_the_shared_umask_before_anything_else(self):
        self.assertEqual(
            f"umask {EXPECTED_UMASK}",
            first_effective_command(AGENT_ENTRYPOINT),
            "the sandbox entrypoint must set the shared-workspace umask before it "
            "creates anything on the PVC",
        )

    def test_credential_sidecar_sets_the_shared_umask_before_anything_else(self):
        self.assertEqual(
            f"umask {EXPECTED_UMASK}",
            first_effective_command(CREDENTIAL_SIDECAR),
            "the credential sidecar must set the shared-workspace umask before it "
            "starts the runtime that executes proxied commands",
        )

    def test_leader_elect_sets_the_shared_umask_before_it_starts_hermes(self):
        """The `replicas > 1` path, which never reaches `agent-entrypoint`.

        Asserted against the parse tree rather than by grepping: what matters is
        that the call is the first statement of `main`, above both the `execvp`
        and the `Popen` that start Hermes. A `umask` further down the function
        runs after the process it was meant to affect has already been replaced.
        """
        tree = ast.parse(LEADER_ELECT.read_text(encoding="utf-8"))
        main = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "main"
            ),
            None,
        )
        self.assertIsNotNone(main, f"{LEADER_ELECT} no longer defines main()")

        for statement in main.body:
            call = statement.value if isinstance(statement, ast.Expr) else None
            if isinstance(call, ast.Call) and ast.unparse(call.func) == "os.umask":
                self.assertEqual(
                    int(EXPECTED_UMASK, 8),
                    ast.literal_eval(call.args[0]),
                    "leader_elect.py sets a umask, but not the one the sidecar "
                    "needs to write in the sandbox's directories",
                )
                return
            # A `global` declaration binds no names at runtime and starts no
            # process, so the umask is allowed to sit below one.
            if isinstance(statement, ast.Global):
                continue
            self.fail(
                "leader_elect.py must call os.umask("
                + EXPECTED_UMASK
                + ") before anything else in main(); found "
                + ast.unparse(statement).splitlines()[0]
            )
        self.fail(f"{LEADER_ELECT} main() never sets a umask")


if __name__ == "__main__":
    unittest.main()
