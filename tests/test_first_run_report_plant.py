"""The first-run-report stack's plant and restore, run against a stub cluster.

`bench/tf/prebuilt/first-run-report/main.tf` plants a fixed ranked report as
INVENTORY.delivered.md on the shell sandbox, backing up a real delivered report
and putting it back on destroy. Pinned here: a fresh plant; a re-apply keeping
its own text; a real report moved to the backup name and the plant written
over it; a re-apply and a stale plant both keeping that backup; the destroy
restoring the backup whatever the path holds, removing the plant only while it
holds the stack's text, keeping a foreign file, and failing when it cannot
check; the refusals (other than one sandbox pod); the terminal login; and the
agent pod's onboarding markers staying untouched. As in
`test_first_install_hello_plant.py`, the provisioners are rendered the way
Terraform renders them and run against a stub `kubectl` and `gcloud`; the stub
runs each in-pod script locally, over one directory per pod.
"""

import base64
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "agents" / "platform" / "scripts"))
import first_run_report  # noqa: E402
_MODULE = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "first-run-report" / "main.tf"
_RESOURCE_RE = re.compile(r'resource "null_resource" "planted_report" \{(.*?)\n\}\n', re.S)
_HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_REPORT_HEREDOC_RE = re.compile(r"planted_report\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_LOCAL_RE = r'\n  {}\s*=\s*"((?:[^"\\]|\\.)*)"'
_REPORT = "/opt/data/INVENTORY.delivered.md"
_BACKUP = "/opt/data/INVENTORY.delivered.md.bench-first-run-report.bak"
_DISPLACED = "/opt/data/INVENTORY.delivered.md.bench-first-run-report.displaced"
_POD_HOME = "/opt/data"
_NAMESPACE = "eval-first-run"
_WORKLOADS = ("ledger-writer", "queue-drainer")

_VARS = {
    "var.project_id": "kube-agents-evals",
    "var.host_cluster_name": "platform-agent-host",
    "var.host_cluster_location": "us-central1",
    "var.agent_namespace": "kubeagents-system",
    "var.sandbox_selector": "app=platform-agent-shell",
    "var.sandbox_container": "shell",
    "var.sandbox_user": "agent",
    "var.workload_namespace": _NAMESPACE,
    "local.first": _WORKLOADS[0],
    "local.second": _WORKLOADS[1],
}
_TRIGGERS = {
    "self.triggers.host_project": _VARS["var.project_id"],
    "self.triggers.host_cluster": _VARS["var.host_cluster_name"],
    "self.triggers.host_location": _VARS["var.host_cluster_location"],
    "self.triggers.namespace": _VARS["var.agent_namespace"],
    "self.triggers.sandbox_selector": _VARS["var.sandbox_selector"],
    "self.triggers.sandbox_container": _VARS["var.sandbox_container"],
    "self.triggers.sandbox_user": "agent",
}

# Runs the in-pod `sh -c` script locally: each sandbox pod's /opt/data is a
# directory under $STATE. A `setpriv` prefix is dropped, since the test user
# owns everything; the call log keeps it.
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
if os.environ.get("ODD_OUTPUT") == "1":
    print("Permission denied")
    sys.exit(0)
cmd = argv[argv.index("--") + 1 :]
if cmd[:1] == ["setpriv"]:
    cmd = cmd[cmd.index("--") + 1 :]
assert cmd[:2] == ["sh", "-c"], cmd
target = next(a for a in argv if a.startswith(("deployment/", "pod/")))
home = state / ("agent" if target.startswith("deployment/") else target.split("/", 1)[1])
home.mkdir(parents=True, exist_ok=True)
args = [a.replace("/opt/data", str(home)) for a in cmd[3:]]
sys.exit(subprocess.run(["sh", "-c", cmd[2]] + args, env=dict(os.environ), timeout=20).returncode)
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
    tests = {name: _local(module, name) for name in ("report_absent", "report_ours", "backup_exists")}
    create_values = {**_VARS, **{f"local.{k}": v for k, v in tests.items()}}
    destroy_values = {**_TRIGGERS, **{f"self.triggers.{k}": v for k, v in tests.items()}}
    planted = _render(textwrap.dedent(_REPORT_HEREDOC_RE.search(module).group(1)) + "\n", _VARS)
    return planted, _render(create, create_values), _render(destroy, destroy_values)


_PLANTED, _CREATE, _DESTROY = _blocks()
_REAL = "# First-run inventory report\n\nThe real delivered report.\n"


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
        bin_dir.mkdir()
        for path, body in ((bin_dir / "kubectl", _KUBECTL_STUB), (bin_dir / "gcloud", "#!/bin/sh\nexit 0\n")):
            path.write_text(body)
            path.chmod(0o755)
        self.calls = root / "calls"
        self.env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            STATE=str(self.state),
            CALLS=str(self.calls),
            REPORT_PATH=_REPORT,
            BACKUP_PATH=_BACKUP,
            DISPLACED_PATH=_DISPLACED,
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

    def _apply(self, **env) -> subprocess.CompletedProcess:
        result = self._run(_CREATE, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _destroy(self, **env) -> subprocess.CompletedProcess:
        result = self._run(_DESTROY, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    @property
    def report(self) -> pathlib.Path:
        return self.sandbox / pathlib.Path(_REPORT).relative_to(_POD_HOME)

    @property
    def backup(self) -> pathlib.Path:
        return self.sandbox / pathlib.Path(_BACKUP).relative_to(_POD_HOME)

    @property
    def displaced(self) -> pathlib.Path:
        return self.sandbox / pathlib.Path(_DISPLACED).relative_to(_POD_HOME)

    # --- apply -------------------------------------------------------------

    def test_plants_the_report_on_the_sandbox(self):
        result = self._apply()
        self.assertIn("Planted", result.stdout)
        self.assertEqual(self.report.read_text(), _PLANTED)
        self.assertFalse(self.backup.exists())
        self.assertEqual(sorted(p.name for p in self.sandbox.iterdir()), [self.report.name])

    def test_the_planted_report_names_both_workloads_as_the_case_grades_them(self):
        for workload in _WORKLOADS:
            self.assertIn(f"{_NAMESPACE}/{workload}", _PLANTED)

    def test_a_re_apply_keeps_its_own_text(self):
        self._apply()
        result = self._apply()
        self.assertIn("already holds this stack's text", result.stdout)
        self.assertEqual(self.report.read_text(), _PLANTED)
        self.assertFalse(self.backup.exists())

    def test_a_real_report_is_backed_up_and_the_plant_written_over_it(self):
        self.report.write_text(_REAL)
        self._apply()
        self.assertEqual(self.report.read_text(), _PLANTED)
        self.assertEqual(self.backup.read_text(), _REAL)

    def test_a_re_apply_over_the_plant_keeps_the_backup(self):
        self.report.write_text(_REAL)
        self._apply()
        self._apply()
        self.assertEqual(self.backup.read_text(), _REAL)
        self.assertEqual(self.report.read_text(), _PLANTED)

    def test_a_stale_plant_beside_a_backup_is_replaced_and_the_backup_kept(self):
        # An earlier run's backup is the real report; a file that is neither
        # ours nor backed up is a stale plant from a build with other text.
        self.backup.write_text(_REAL)
        self.report.write_text("an older build's planted text\n")
        self._apply()
        self.assertEqual(self.report.read_text(), _PLANTED)
        self.assertEqual(self.backup.read_text(), _REAL)

    def test_a_symlink_at_the_path_is_backed_up_not_followed(self):
        target = self.sandbox / "elsewhere"
        target.write_text(_REAL)
        self.report.symlink_to(target)
        self._apply()
        self.assertFalse(self.report.is_symlink())
        self.assertEqual(self.report.read_text(), _PLANTED)
        self.assertTrue(self.backup.is_symlink())
        self.assertEqual(target.read_text(), _REAL)

    def test_runs_in_the_sandbox_as_the_terminal_login(self):
        self._apply()
        self._destroy()
        execs = [line for line in self.calls.read_text().splitlines() if line.startswith("exec")]
        self.assertEqual(len(execs), 2)
        for line in execs:
            self.assertIn("-- setpriv --reuid=agent --regid=agent --init-groups -- sh -c", line)

    def test_refuses_without_exactly_one_sandbox_pod(self):
        for pods in ("0", "2"):
            with self.subTest(pods=pods):
                result = self._run(_CREATE, SANDBOX_PODS=pods)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"found {pods}", result.stderr)
                self.assertFalse(self.report.exists())

    def test_apply_fails_on_an_answer_it_does_not_know(self):
        result = self._run(_CREATE, ODD_OUTPUT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not plant", result.stderr)

    def test_the_agent_pod_is_never_touched(self):
        self.agent.joinpath(".bootstrap_completed").write_text("no-human\n")
        self._apply()
        self._destroy()
        self.assertEqual(sorted(p.name for p in self.agent.iterdir()), [".bootstrap_completed"])
        self.assertEqual(self.agent.joinpath(".bootstrap_completed").read_text(), "no-human\n")
        self.assertNotIn("deployment/", self.calls.read_text())

    # --- destroy -----------------------------------------------------------

    def test_destroy_removes_the_planted_report(self):
        self._apply()
        result = self._destroy()
        self.assertIn("removed", result.stdout)
        self.assertEqual(list(self.sandbox.iterdir()), [])

    def test_destroy_passes_when_the_file_is_gone(self):
        result = self._destroy()
        self.assertIn("absent", result.stdout)

    def test_destroy_restores_the_backup_over_the_plant(self):
        self.report.write_text(_REAL)
        self._apply()
        result = self._destroy()
        self.assertIn("restored", result.stdout)
        self.assertEqual(self.report.read_text(), _REAL)
        self.assertFalse(self.backup.exists())

    def test_destroy_sets_aside_a_report_delivered_during_the_run_before_restoring(self):
        # A real delivery during the run renamed its report over the plant: the
        # backup still goes back, and that newer report is kept at a side name
        # the destroy prints, rather than overwritten.
        self.backup.write_text(_REAL)
        self.report.write_text("delivered during the run\n")
        result = self._destroy()
        self.assertEqual(self.report.read_text(), _REAL)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.displaced.read_text(), "delivered during the run\n")
        self.assertIn(_DISPLACED, result.stdout)

    def test_destroy_restores_the_backup_over_the_plant_without_a_side_copy(self):
        self.report.write_text(_REAL)
        self._apply()
        self._destroy()
        self.assertEqual(self.report.read_text(), _REAL)
        self.assertFalse(self.displaced.exists())

    def test_destroy_keeps_a_foreign_file_with_no_backup(self):
        self.report.write_text("A REAL RANKED REPORT\n")
        result = self._destroy()
        self.assertEqual(self.report.read_text(), "A REAL RANKED REPORT\n")
        self.assertIn("kept", result.stdout)

    def test_destroy_fails_on_an_answer_it_does_not_know(self):
        self._apply()
        result = self._run(_DESTROY, ODD_OUTPUT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not restore", result.stderr)

    def test_destroy_fails_without_a_sandbox_pod(self):
        result = self._run(_DESTROY, SANDBOX_PODS="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not be restored", result.stderr)

    def test_the_plant_is_the_delivered_name_the_tool_reads_first(self):
        # The path the stack plants on, the path this test drives, and the
        # name first_run_report reads first are one path.
        expected = f"{first_run_report.SANDBOX_HOME}/{first_run_report.DELIVERED_REPORT_NAME}"
        self.assertEqual(_REPORT, expected)
        self.assertEqual(_local(_MODULE.read_text(), "report_path"), expected)

    def test_apply_and_destroy_share_one_ownership_test(self):
        module = _MODULE.read_text()
        self.assertEqual(module.count("[ -f "), 1)
        self.assertIn("${local.report_ours}", module)
        self.assertIn("${self.triggers.report_ours}", module)
        self.assertIn("${local.backup_exists}", module)
        self.assertIn("${self.triggers.backup_exists}", module)
        self.assertIn("DISPLACED_PATH = self.triggers.displaced_path", module)


if __name__ == "__main__":
    unittest.main()
