"""The presubmit's section 7c: the agent has the plugins its image ships.

The entrypoint copies every plugin folder of the image into the home's plugins
folder and only warns when a copy fails, so an agent whose copy failed on a
new data volume is Ready with none of them. The gate reads the two folders in
the agent container and fails the deploy when the home lacks one.

This file holds the gate's two paths to the entrypoint's, and runs the real
block: once against a stub `kubectl` for the read that never works, and with a
`kubectl` that runs the pod's script here, against two folders on disk.
"""

import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_ENTRYPOINT = _REPO_ROOT / "deploy" / "shared" / "docker-entrypoint.sh"
_GATE_START = "shipped_plugins_read=false"
_GATE_END = 'echo "✓ The agent has the plugins its image ships'
_GATE_NAMESPACE = "kubeagents-system"
_GATE_PASSED = "GATE_PASSED"
_IMAGE_DIR_CONSTANT = "SHIPPED_PLUGINS_IMAGE_DIR"
_HOME_DIR_CONSTANT = "SHIPPED_PLUGINS_HOME_DIR"
_ENTRYPOINT_IMAGE_DIR_RE = re.compile(r'^readonly DEFAULTS_PLUGINS_DIR="([^"]+)"$', re.MULTILINE)
_ENTRYPOINT_HOME_RE = re.compile(r'^export TARGET_DIR="\$\{PLATFORM_AGENT_HOME:-([^}]+)\}"$', re.MULTILINE)
_HOME_PLUGINS_SUBDIR = "plugins"
_EXEC_ARGS_FILE = "exec-args.txt"
# A `kubectl` whose exec runs the command after `--` on this machine, and
# records what came before it: the namespace, the workload and the container.
_KUBECTL_RUNS_EXEC_HERE = """#!/usr/bin/env bash
if [ "$1" = "exec" ]; then
  : > "$(dirname "$0")/../exec-args.txt"
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do printf '%s\\n' "$1" >> "$(dirname "$0")/../exec-args.txt"; shift; done
  shift
  exec "$@"
fi
exit 0
"""
# A `kubectl` whose exec always fails, as for a pod that is gone.
_KUBECTL_EXEC_FAILS = """#!/usr/bin/env bash
if [ "$1" = "exec" ]; then
  echo "error: unable to upgrade connection" >&2
  exit 1
fi
exit 0
"""


def _shell_constant(name: str) -> str:
    match = re.search(rf'^readonly {name}="?([^"\n]*)"?$', _CI_DEPLOY.read_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"hack/ci-deploy.sh declares no readonly {name}")
    return match.group(1)


def _head_constants(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line.startswith("readonly "))


def _gate(text: str) -> str:
    start = text.find(_GATE_START)
    assert start != -1, f"{_GATE_START!r} not found in hack/ci-deploy.sh"
    end = text.find(_GATE_END, start)
    assert end != -1, f"{_GATE_END!r} not found after {_GATE_START!r}"
    return text[start:end]


class TheGateReadsTheEntrypointsFoldersTest(unittest.TestCase):
    def test_the_image_folder_is_the_one_the_entrypoint_copies_from(self) -> None:
        match = _ENTRYPOINT_IMAGE_DIR_RE.search(_ENTRYPOINT.read_text())
        self.assertIsNotNone(match, "docker-entrypoint.sh no longer declares DEFAULTS_PLUGINS_DIR")
        self.assertEqual(match.group(1), _shell_constant(_IMAGE_DIR_CONSTANT))

    def test_the_home_folder_is_the_one_the_entrypoint_copies_into(self) -> None:
        match = _ENTRYPOINT_HOME_RE.search(_ENTRYPOINT.read_text())
        self.assertIsNotNone(match, "docker-entrypoint.sh no longer declares TARGET_DIR with its default")
        self.assertEqual(f"{match.group(1)}/{_HOME_PLUGINS_SUBDIR}", _shell_constant(_HOME_DIR_CONSTANT))


class TheGateFailsWhenTheHomeLacksAShippedPluginTest(unittest.TestCase):
    def _run_gate(self, kubectl: str, image_plugins=(), home_plugins=()):
        text = _CI_DEPLOY.read_text()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            image_dir = tmp_path / "image"
            home_dir = tmp_path / "home"
            for root, names in ((image_dir, image_plugins), (home_dir, home_plugins)):
                root.mkdir()
                for name in names:
                    (root / name).mkdir()
            # A file at the top of the image folder is not a plugin.
            (image_dir / "README.md").write_text("not a plugin\n", encoding="utf-8")
            kubectl_stub = bin_dir / "kubectl"
            kubectl_stub.write_text(kubectl, encoding="utf-8")
            sleep_stub = bin_dir / "sleep"
            sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            for stub in (kubectl_stub, sleep_stub):
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            # The gate's own constants, with the two folders pointed at the ones on disk.
            constants = _head_constants(text)
            for name, folder in ((_IMAGE_DIR_CONSTANT, image_dir), (_HOME_DIR_CONSTANT, home_dir)):
                line = f'readonly {name}="{_shell_constant(name)}"'
                self.assertIn(line, constants)
                constants = constants.replace(line, f'readonly {name}="{folder}"')
            script = "\n".join(
                (
                    "set -euo pipefail",
                    constants,
                    f"NAMESPACE={_GATE_NAMESPACE}",
                    _gate(text),
                    f'echo "{_GATE_PASSED}"',
                )
            )
            proc = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, cwd=_REPO_ROOT,
                env=get_isolated_test_env(bin_dir=bin_dir),
            )
            args_file = tmp_path / _EXEC_ARGS_FILE
            proc.exec_args = args_file.read_text(encoding="utf-8").split() if args_file.exists() else []
            return proc

    def test_a_home_with_every_shipped_plugin_passes(self) -> None:
        # The home may hold more: a mounted plugin is a folder the image does not ship.
        proc = self._run_gate(
            _KUBECTL_RUNS_EXEC_HERE,
            image_plugins=("agent_roster", "common", "hermes_otel"),
            home_plugins=("agent_roster", "common", "hermes_otel", "pubsubplatform"),
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn(_GATE_PASSED, proc.stdout)

    def test_a_home_with_only_the_mounted_plugin_fails_and_names_each_missing_one(self) -> None:
        """The state of #6279: the home's folder holds the mount point and nothing the image ships."""
        proc = self._run_gate(
            _KUBECTL_RUNS_EXEC_HERE,
            image_plugins=("agent_roster", "common", "hermes_otel"),
            home_plugins=("pubsubplatform",),
        )
        self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertNotIn(_GATE_PASSED, proc.stdout)
        self.assertIn("runs without plugins its image ships: agent_roster common hermes_otel", proc.stdout)

    def test_one_missing_plugin_fails(self) -> None:
        proc = self._run_gate(
            _KUBECTL_RUNS_EXEC_HERE,
            image_plugins=("agent_roster", "common"),
            home_plugins=("agent_roster",),
        )
        self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("runs without plugins its image ships: common", proc.stdout)

    def test_the_read_is_of_the_agent_container_of_the_gateway(self) -> None:
        proc = self._run_gate(_KUBECTL_RUNS_EXEC_HERE, image_plugins=("common",), home_plugins=("common",))
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        gateway = _shell_constant("AGENT_DEPLOYMENT_NAME").replace(
            "${PLATFORM_AGENT_CR_NAME}", _shell_constant("PLATFORM_AGENT_CR_NAME"))
        self.assertEqual(
            proc.exec_args,
            ["exec", "-n", _GATE_NAMESPACE, f"deployment/{gateway}", "-c", _shell_constant("PUBSUB_RELAY_GATEWAY_CONTAINER")],
        )

    def test_an_image_folder_with_no_plugin_folder_is_a_failed_read(self) -> None:
        proc = self._run_gate(_KUBECTL_RUNS_EXEC_HERE, image_plugins=(), home_plugins=("pubsubplatform",))
        self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertNotIn(_GATE_PASSED, proc.stdout)
        self.assertIn("could not read the agent's plugin folders", proc.stdout)

    def test_a_read_that_never_works_fails_and_says_so(self) -> None:
        proc = self._run_gate(_KUBECTL_EXEC_FAILS)
        self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("could not read the agent's plugin folders", proc.stdout)
        self.assertNotIn("runs without plugins", proc.stdout)


if __name__ == "__main__":
    unittest.main()
