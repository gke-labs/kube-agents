# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The first-install-hello stack's report plant, run against a stub cluster.

For variant from_report, `bench/tf/prebuilt/first-install-hello/main.tf`
plants INVENTORY.md on the shell sandbox. Pinned here: the refusals (a person
waiting on an undelivered report, a ranking card filed and not delivered, other
than one sandbox pod) stopping the apply before anything is written; a report
the stack did not plant being left as it is; a file holding the stack's own
text being kept; a report appearing during the write failing the apply; and the
destroy removing the report only while it holds that text, and failing when it
cannot check. As in
`test_bootstrap_ranking_plant.py`, the provisioners are rendered the way
Terraform renders them and run against a stub `kubectl` and `gcloud`; the stub
runs each in-pod script locally, over one directory per pod.
"""

import base64
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "first-install-hello" / "main.tf"
_RESOURCE_RE = re.compile(r'resource "null_resource" "planted_report" \{(.*?)\n\}\n', re.S)
_HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_LOCAL_RE = r'\n  {}\s*=\s*"((?:[^"\\]|\\.)*)"'
_SUFFIX = "report-ready"
_REPORT = "/opt/data/INVENTORY.md"
_POD_HOME = "/opt/data"

_VARS = {
    "var.project_id": "kube-agents-evals",
    "var.host_cluster_name": "platform-agent-host",
    "var.host_cluster_location": "us-central1",
    "var.agent_namespace": "kubeagents-system",
    "var.agent_deployment": "platform-agent-gateway",
    "var.agent_container": "platform-agent",
    "var.sandbox_selector": "app=platform-agent-shell",
    "var.sandbox_container": "shell",
    "var.case_suffix": _SUFFIX,
}
_TRIGGERS = {
    "self.triggers.host_project": _VARS["var.project_id"],
    "self.triggers.host_cluster": _VARS["var.host_cluster_name"],
    "self.triggers.host_location": _VARS["var.host_cluster_location"],
    "self.triggers.namespace": _VARS["var.agent_namespace"],
    "self.triggers.sandbox_selector": _VARS["var.sandbox_selector"],
    "self.triggers.sandbox_container": _VARS["var.sandbox_container"],
}

# Runs the in-pod `sh -c` script locally: the agent pod's home and each sandbox
# pod's /opt/data are directories under $STATE, and `chown`/`stat` are stubbed,
# since the test user owns everything and macOS's stat has no -c.
_KUBECTL_STUB = r'''#!/usr/bin/env python3
import os, pathlib, subprocess, sys

argv = sys.argv[1:]
state = pathlib.Path(os.environ["STATE"])
with open(os.environ["CALLS"], "a") as fh:
    fh.write(" ".join(argv).replace("\n", " ") + "\n")
if argv[:1] == ["get"]:
    for n in range(int(os.environ.get("SANDBOX_PODS", "1"))):
        print("pod/platform-agent-shell-%d" % n)
    sys.exit(0)
if os.environ.get("ODD_OUTPUT") == "1" and "base64 -d" in argv[-3]:
    print("Permission denied")
    sys.exit(0)
if os.environ.get("EXEC_FAIL") == "1":
    sys.stderr.write("error: unable to upgrade connection: container not found\n")
    sys.exit(1)
cmd = argv[argv.index("--") + 1 :]
assert cmd[:2] == ["sh", "-c"], cmd
target = next(a for a in argv if a.startswith(("deployment/", "pod/")))
home = state / ("agent" if target.startswith("deployment/") else target.split("/", 1)[1])
home.mkdir(parents=True, exist_ok=True)
args = [a.replace("/opt/data", str(home)) for a in cmd[3:]]
env = dict(os.environ, PLATFORM_AGENT_HOME=str(home), PATH=os.environ["FAKEBIN"] + ":" + os.environ["PATH"])
sys.exit(subprocess.run(["sh", "-c", cmd[2]] + args, env=env).returncode)
'''


def _render(text: str, values: dict) -> str:
    """`text` with Terraform's interpolations replaced and `$${` unescaped."""
    escaped = "\0"
    text = text.replace("$${", escaped)
    text = re.sub(r"\$\{([^}]*)\}", lambda m: values[m.group(1).strip()], text)
    return text.replace(escaped, "${")


def _local(module: str, name: str) -> str:
    """A string local's value, unescaped as Terraform reads it."""
    raw = re.search(_LOCAL_RE.format(name), module).group(1)
    return _render(raw, _VARS).encode().decode("unicode_escape")


def _blocks() -> tuple[str, str, str]:
    """The planted text, and the create and destroy provisioner scripts."""
    module = _MODULE.read_text()
    resource = _RESOURCE_RE.search(module).group(1)
    create, destroy = (textwrap.dedent(b) + "\n" for b in _HEREDOC_RE.findall(resource))
    tests = {name: _local(module, name) for name in ("report_absent", "report_ours")}
    create_values = {**_VARS, **{f"local.{k}": v for k, v in tests.items()}}
    destroy_values = {**_TRIGGERS, **{f"self.triggers.{k}": v for k, v in tests.items()}}
    return _local(module, "planted_report"), _render(create, create_values), _render(destroy, destroy_values)


_PLANTED, _CREATE, _DESTROY = _blocks()


class PlantTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.state = root / "state"
        self.agent = self.state / "agent"
        self.sandbox = self.state / "platform-agent-shell-0"
        self.agent.mkdir(parents=True)
        self.sandbox.mkdir(parents=True)
        bin_dir = root / "bin"
        fakebin = root / "fakebin"
        bin_dir.mkdir()
        fakebin.mkdir()
        for path, body in (
            (bin_dir / "kubectl", _KUBECTL_STUB),
            (bin_dir / "gcloud", "#!/bin/sh\nexit 0\n"),
            (fakebin / "chown", "#!/bin/sh\nexit 0\n"),
            (fakebin / "stat", "#!/bin/sh\necho 0:0\n"),
            # A report that lands between the check and the link.
            (fakebin / "ln", '#!/bin/sh\nif [ "$RACE" = 1 ]; then echo raced > "$2"; exit 1; fi\nexec /bin/ln "$@"\n'),
        ):
            path.write_text(body)
            path.chmod(0o755)
        self.calls = root / "calls"
        self.env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            FAKEBIN=str(fakebin),
            STATE=str(self.state),
            CALLS=str(self.calls),
            REPORT_PATH=_REPORT,
            REPORT_B64=base64.b64encode(_PLANTED.encode()).decode(),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, script: str, **env) -> subprocess.CompletedProcess:
        if shutil.which("bash") is None:
            self.skipTest("bash is not installed")
        return subprocess.run(
            ["bash", "-c", script], env={**self.env, **env}, capture_output=True, text=True, timeout=60
        )

    @property
    def report(self) -> pathlib.Path:
        return self.sandbox / pathlib.Path(_REPORT).relative_to(_POD_HOME)

    # --- apply -------------------------------------------------------------

    def test_plants_the_report_on_the_sandbox(self):
        result = self._run(_CREATE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report.read_text(), _PLANTED)
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o644)
        self.assertIn(_SUFFIX, _PLANTED)

    def test_refuses_while_a_person_waits_on_an_undelivered_report(self):
        (self.agent / ".user_aligned").touch()
        result = self._run(_CREATE)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("a person has connected", result.stderr)
        self.assertFalse(self.report.exists())

    def test_plants_once_onboarding_has_delivered(self):
        (self.agent / ".user_aligned").touch()
        (self.agent / ".bootstrap_completed").touch()
        result = self._run(_CREATE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.report.exists())

    def test_refuses_while_a_ranking_card_is_filed_and_undelivered(self):
        (self.agent / ".bootstrap_handoff_filed").write_text("sweep=t_sweep\ntask_id=t_rank\nfiled_at=1\n")
        result = self._run(_CREATE)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ranking card", result.stderr)
        self.assertFalse(self.report.exists())

    def test_plants_when_the_hand_off_filed_no_ranking_card(self):
        (self.agent / ".bootstrap_handoff_filed").write_text("sweep=t_sweep\ntask_id=none\nfiled_at=1\n")
        result = self._run(_CREATE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report.read_text(), _PLANTED)

    def test_plants_after_delivery_whatever_the_hand_off_filed(self):
        (self.agent / ".bootstrap_handoff_filed").write_text("task_id=t_rank\n")
        (self.agent / ".bootstrap_completed").touch()
        result = self._run(_CREATE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.report.exists())

    def test_fails_when_a_report_lands_during_the_write(self):
        result = self._run(_CREATE, RACE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not plant", result.stderr)
        self.assertEqual(self.report.read_text(), "raced\n")
        self.assertEqual([p.name for p in self.sandbox.iterdir()], ["INVENTORY.md"])

    def test_refuses_when_the_agent_pod_cannot_be_read(self):
        result = self._run(_CREATE, EXEC_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.report.exists())

    def test_leaves_a_report_it_did_not_plant(self):
        self.report.write_text("A REAL RANKED REPORT\n")
        result = self._run(_CREATE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plants and removes nothing", result.stdout)
        self.assertEqual(self.report.read_text(), "A REAL RANKED REPORT\n")

    def test_leaves_a_symlink_at_the_path(self):
        target = self.sandbox / "elsewhere"
        target.write_text(_PLANTED)
        self.report.symlink_to(target)
        result = self._run(_CREATE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.report.is_symlink())
        self.assertEqual(target.read_text(), _PLANTED)

    def test_keeps_a_report_an_earlier_run_left(self):
        self.report.write_text(_PLANTED)
        result = self._run(_CREATE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kept", result.stdout)
        self.assertEqual(self.report.read_text(), _PLANTED)

    def test_refuses_without_exactly_one_sandbox_pod(self):
        for pods in ("0", "2"):
            with self.subTest(pods=pods):
                result = self._run(_CREATE, SANDBOX_PODS=pods)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"found {pods}", result.stderr)
                self.assertFalse(self.report.exists())

    # --- destroy -----------------------------------------------------------

    def test_destroy_removes_the_planted_report(self):
        self.report.write_text(_PLANTED)
        result = self._run(_DESTROY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.report.exists())
        self.assertIn("removed", result.stdout)

    def test_destroy_keeps_a_report_written_since(self):
        self.report.write_text("A REAL RANKED REPORT\n")
        result = self._run(_DESTROY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report.read_text(), "A REAL RANKED REPORT\n")
        self.assertIn("kept", result.stdout)

    def test_destroy_passes_when_delivery_moved_the_report_aside(self):
        result = self._run(_DESTROY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("absent", result.stdout)

    def test_destroy_keeps_a_symlink_at_the_path(self):
        target = self.sandbox / "elsewhere"
        target.write_text(_PLANTED)
        self.report.symlink_to(target)
        result = self._run(_DESTROY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.report.is_symlink())
        self.assertEqual(target.read_text(), _PLANTED)
        self.assertIn("kept", result.stdout)

    def test_destroy_fails_on_an_answer_it_does_not_know(self):
        self.report.write_text(_PLANTED)
        result = self._run(_DESTROY, ODD_OUTPUT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not remove", result.stderr)

    def test_apply_and_destroy_share_one_ownership_test(self):
        module = _MODULE.read_text()
        self.assertEqual(module.count("[ -f "), 1)
        self.assertIn("${local.report_ours}", module)
        self.assertIn("${self.triggers.report_ours}", module)

    def test_destroy_fails_without_a_sandbox_pod(self):
        result = self._run(_DESTROY, SANDBOX_PODS="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no shell sandbox pod", result.stderr)

    def test_destroy_fails_when_the_sandbox_cannot_be_read(self):
        self.report.write_text(_PLANTED)
        result = self._run(_DESTROY, EXEC_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.report.exists())


if __name__ == "__main__":
    unittest.main()
