"""Contract tests for what the agent image guarantees about its own startup.

Run: python3 -m unittest discover -s deploy/docker -p 'test_*.py'

Two guarantees are pinned here, both invisible from any single source file:

  * the process the entrypoint execs starts inside the shared workspace, so the
    credential-proxy shims are not refused before they run; and
  * the vendored Python tree carries a bytecode cache, since the runtime cannot
    build one.

The entrypoint assertions run the real script rather than reading it. Every
step it takes is gated on an absolute image path (/opt/hermes, /opt/defaults)
that does not exist on a test machine, so it walks its guards and reaches the
exec with no image present — which makes the honest test available, and a
regex over the file the weaker substitute.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "deploy" / "shared" / "docker-entrypoint.sh"
DOCKERFILE = REPO_ROOT / "deploy" / "docker" / "Dockerfile"
MANIFESTS_GO = (
    REPO_ROOT
    / "k8s-operator"
    / "internal"
    / "controller"
    / "platformagent_manifests.go"
)

# The default both sides have to agree on. The proxy refuses a cwd outside
# CREDENTIAL_PROXY_WORKSPACE_ROOT, so if the entrypoint's default home and the
# operator's diverge, the agent starts in a directory its own shims reject.
DEFAULT_AGENT_HOME = "/opt/data"


def platform_stage(dockerfile_text: str) -> str:
    """The Dockerfile text belonging to the `platform` target."""
    stages = re.split(r"^FROM .*? AS (\S+)\s*$", dockerfile_text, flags=re.M)
    # re.split with one group yields [pre, name, body, name, body, ...].
    for name, body in zip(stages[1::2], stages[2::2]):
        if name == "platform":
            return body
    raise AssertionError("no `FROM … AS platform` stage in the Dockerfile")


class EntrypointStartsInsideTheWorkspaceTest(unittest.TestCase):
    """The exec'd process must not inherit a cwd the credential proxy rejects.

    The image inherits WORKDIR /opt/hermes from the upstream base. Every
    credentialed CLI (kubectl, gcloud, gh, git) is a PATH shim that posts
    os.getcwd() with each request, and the proxy refuses any cwd outside the
    workspace root — so launched from /opt/hermes, even `kubectl version
    --client` fails before it runs. The cwd is the only lever that reaches every
    caller: Hermes resolves the terminal and execute_code directories from a
    ladder ending at os.getcwd(), the local backend overwrites any configured
    terminal.cwd with it, and kanban workers inherit it as child processes.
    """

    def run_entrypoint(self, home: Path, start_in: Path):
        """Run the entrypoint from `start_in` and return the exec'd cwd."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PLATFORM_AGENT_HOME": str(home),
        }
        result = subprocess.run(
            ["sh", str(ENTRYPOINT), "sh", "-c", "pwd"],
            cwd=str(start_in),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode, 0, f"entrypoint failed:\n{result.stderr}"
        )
        return result.stdout.strip().splitlines()[-1]

    def test_the_exec_d_process_starts_in_the_agent_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            elsewhere = Path(tmp) / "elsewhere"
            home.mkdir()
            elsewhere.mkdir()
            # `elsewhere` stands in for /opt/hermes: a real directory outside
            # the workspace, which is exactly where the container would start.
            self.assertEqual(
                Path(self.run_entrypoint(home, elsewhere)).resolve(),
                home.resolve(),
            )

    def test_it_does_not_simply_inherit_the_starting_directory(self):
        # The pre-fix behaviour, stated as its own assertion so that a change
        # which quietly drops the `cd` fails here with an obvious message
        # rather than only in the test above.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            elsewhere = Path(tmp) / "elsewhere"
            home.mkdir()
            elsewhere.mkdir()
            self.assertNotEqual(
                Path(self.run_entrypoint(home, elsewhere)).resolve(),
                elsewhere.resolve(),
            )

    def test_the_change_of_directory_cannot_kill_the_container(self):
        # The script runs under `set -e`, so an unguarded `cd` to a missing
        # directory would abort the container outright. A pod that cannot reach
        # its workspace should still come up and say so.
        text = ENTRYPOINT.read_text()
        opener = 'if ! cd "$TARGET_DIR"; then'
        self.assertIn(
            opener,
            text,
            # Named rather than dumped: the script is 300 lines, and an
            # assertion that prints all of them buries what it is asking for.
            f"docker-entrypoint.sh does not guard its cd with `{opener}`",
        )
        self.assertIn(
            "WARN",
            text.split(opener, 1)[1].split("fi", 1)[0],
            "the guarded cd fails silently; a broken mount should say so",
        )

    def test_the_default_home_is_the_one_the_operator_uses(self):
        # The two are set independently — the entrypoint from its own default,
        # CREDENTIAL_PROXY_WORKSPACE_ROOT by the operator — and the fix is only
        # correct while they agree.
        self.assertIn(
            f'TARGET_DIR="${{PLATFORM_AGENT_HOME:-{DEFAULT_AGENT_HOME}}}"',
            ENTRYPOINT.read_text(),
        )
        go = MANIFESTS_GO.read_text()
        self.assertIn(f'homeDir := "{DEFAULT_AGENT_HOME}"', go)
        self.assertRegex(
            go,
            r'Name:\s*"CREDENTIAL_PROXY_WORKSPACE_ROOT",\s*Value:\s*homeDir',
        )


class BytecodeIsBakedAtBuildTimeTest(unittest.TestCase):
    """The image must ship the cache the runtime is unable to produce.

    The base image sets PYTHONDONTWRITEBYTECODE=1 and /opt/hermes is read-only
    to the pod's uid, so nothing can write a __pycache__ at runtime: a live pod
    shows 9366 .py files under /opt/hermes and 0 .pyc. Every Python process the
    container starts therefore re-parses the whole tree from source.
    """

    def setUp(self):
        self.stage = platform_stage(DOCKERFILE.read_text())

    def compileall_command(self):
        """The whole RUN instruction that precompiles, continuations joined.

        A shell command in a Dockerfile is wrapped across physical lines with
        trailing backslashes, and the parts that matter here — the interpreter
        and the `||` fallback — sit on different ones.
        """
        instructions = []
        current = []
        for line in self.stage.splitlines():
            if line.lstrip().startswith("#"):
                continue
            current.append(line.rstrip().removesuffix("\\"))
            if not line.rstrip().endswith("\\"):
                instructions.append(" ".join(current))
                current = []
        for instruction in instructions:
            if "compileall" in instruction:
                return instruction
        self.fail("the platform stage does not precompile /opt/hermes")

    def test_the_platform_stage_precompiles_the_vendored_tree(self):
        self.assertIn("/opt/hermes", self.compileall_command())

    def test_it_compiles_with_the_interpreter_that_will_import(self):
        # A .pyc is only loaded by an interpreter whose magic tag matches the
        # one that wrote it, so a bare `python3` here would bake a cache the
        # venv silently ignores.
        self.assertIn("/opt/hermes/.venv/bin/python3", self.compileall_command())

    def test_it_does_not_ask_for_optimized_bytecode(self):
        # The runtime sets neither PYTHONOPTIMIZE nor -O, so it looks for the
        # plain .cpython-NNN.pyc. An `-o` here would write .opt-N files that
        # nothing loads.
        self.assertNotRegex(
            self.compileall_command(), r"\s-o\b|\s--optimize\b"
        )

    def test_a_module_that_will_not_compile_does_not_break_the_build(self):
        # Large vendored trees carry the odd file that cannot be compiled. The
        # cache is an optimisation; losing part of it is today's behaviour, and
        # not a reason to fail the image.
        self.assertIn("||", self.compileall_command())


class BytecodePremiseTest(unittest.TestCase):
    """The interpreter behaviour the build step depends on.

    Baking the cache only helps if PYTHONDONTWRITEBYTECODE blocks *writing* and
    not *reading*, and only works if compileall ignores the flag. Both are
    documented CPython behaviour rather than anything this repo controls, which
    is exactly why they are worth pinning: if a future base image changes
    either, the Dockerfile step becomes a silent no-op.
    """

    def setUp(self):
        # Some distributions patch cache_from_source to redirect __pycache__
        # out of the source tree — macOS's system Python writes to
        # ~/Library/Caches instead. Such an interpreter cannot answer a
        # question about in-tree bytecode, and quietly passing would be worse
        # than not running.
        probe = importlib.util.cache_from_source("/tmp/probe.py")
        if not probe.startswith("/tmp/"):
            self.skipTest(
                f"{sys.executable} redirects the bytecode cache to {probe}; "
                "the container's interpreter does not"
            )
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = Path(self.tmp) / "pkg"
        self.pkg.mkdir()
        (self.pkg / "__init__.py").write_text("")
        (self.pkg / "mod.py").write_text("X = 1\n")

    def compileall(self):
        return subprocess.run(
            [sys.executable, "-m", "compileall", "-q", self.tmp],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_compileall_writes_a_cache_even_when_writes_are_suppressed(self):
        result = self.compileall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            list(self.pkg.glob("__pycache__/mod.*.pyc")),
            "compileall honoured PYTHONDONTWRITEBYTECODE and wrote nothing; "
            "the Dockerfile step would bake no cache at all",
        )

    def test_an_existing_cache_is_read_even_when_writes_are_suppressed(self):
        self.assertEqual(self.compileall().returncode, 0)

        # Rewrite the source to a different value, keeping its length and mtime
        # so the cache still validates. If the import returns the OLD value,
        # the .pyc was used; if the new one, the cache was ignored.
        source = self.pkg / "mod.py"
        stat = source.stat()
        source.write_text("X = 2\n")
        os.utime(source, (stat.st_atime, stat.st_mtime))

        result = subprocess.run(
            [sys.executable, "-c", "from pkg import mod; print(mod.X)"],
            cwd=self.tmp,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "1",
            "the baked cache was ignored and the source recompiled",
        )

    def test_a_stale_cache_is_rejected_rather_than_trusted(self):
        # The other half of the safety argument: the baked .pyc is validated
        # against its source, so it cannot serve stale code if the tree is
        # ever rebuilt on top of it.
        self.assertEqual(self.compileall().returncode, 0)
        source = self.pkg / "mod.py"
        source.write_text("X = 99\n")  # mtime moves this time.

        result = subprocess.run(
            [sys.executable, "-c", "from pkg import mod; print(mod.X)"],
            cwd=self.tmp,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.stdout.strip(), "99")


if __name__ == "__main__":
    unittest.main()
