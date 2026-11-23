"""The fleet-audit report scripts in the worker image (#5623).

a2a/Dockerfile.worker copies the fleet-audit AgentProfile's report scripts into
/opt/kube-agents/scripts/fleet-audit from their sources at build time, flat, so a script's
own directory is where Python finds the modules it imports. This holds the COPY
to exactly that set of sources, and checks the set imports from one directory
alone, as the image's build-time check does, without building the image.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_DOCKERFILE = REPO_ROOT / "a2a" / "Dockerfile.worker"
DEST = "/opt/kube-agents/scripts/fleet-audit/"

# audit_report.py and the modules its finish paths import: the closure a
# profile worker needs, and no more.
SOURCES = (
    "agents/platform/skills/fleet-audit/scripts/audit_report.py",
    "agents/platform/skills/fleet-audit/scripts/report_status.py",
    "agents/platform/skills/fleet-audit/scripts/manifest_delta.py",
    "agents/platform/scripts/gitops_workspace.py",
    "agents/platform/scripts/repo_ref.py",
    "agents/platform/scripts/gitops_preflight.py",
    "agents/platform/scripts/credential_proxy_client.py",
    "agents/platform/scripts/vcs_client.py",
)


def scripts_dir(tmp: str) -> Path:
    """The image's own layout under tmp, <tmp>/opt/kube-agents/scripts/fleet-audit:
    audit_report.py reads Path(__file__).parents[3] at import, which a
    shallow directory doesn't have."""
    d = Path(tmp) / DEST.strip("/")
    d.mkdir(parents=True)
    return d


def copy_sources(dockerfile: str) -> list[str]:
    """The sources of the COPY whose destination is DEST."""
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    for line in joined.splitlines():
        words = line.split()
        if words[:1] == ["COPY"] and words[-1] == DEST:
            return [w for w in words[1:-1] if not w.startswith("--")]
    return []


DISPATCH_GO = REPO_ROOT / "a2a" / "lib" / "dispatch.go"
AGENTPROFILE_TYPES_GO = REPO_ROOT / "k8s-operator" / "api" / "v1alpha1" / "agentprofile_types.go"


def go_string_const(path: Path, name: str) -> str:
    m = re.search(r"^const " + re.escape(name) + r'\s*=\s*"([^"]+)"', path.read_text(), re.M)
    if not m:
        raise AssertionError(f"no const {name} in {path}")
    return m.group(1)


class WorkerFleetAuditScriptsTest(unittest.TestCase):
    def test_the_scripts_are_under_the_helper_script_root(self):
        """A script row (spec.scripts[].path) may name only a path under the one
        helper script root: lib.ScriptRoot for the dispatcher and the worker, and
        AgentProfileScriptRoot for the CRD's validation. The scripts' directory
        stays under both, so the two can't drift apart."""
        for path, name in ((DISPATCH_GO, "ScriptRoot"), (AGENTPROFILE_TYPES_GO, "AgentProfileScriptRoot")):
            root = go_string_const(path, name)
            self.assertTrue(root.endswith("/"), f"{name} = {root!r}")
            self.assertTrue(DEST.startswith(root), f"{DEST} is not under {name} = {root!r}")

    def test_the_copy_names_exactly_the_closure(self):
        self.assertEqual(sorted(copy_sources(WORKER_DOCKERFILE.read_text())), sorted(SOURCES))

    def test_the_closure_imports_from_one_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = scripts_dir(tmp)
            for src in SOURCES:
                shutil.copy(REPO_ROOT / src, d / Path(src).name)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "PYTHONDONTWRITEBYTECODE": "1"}
            modules = [Path(s).stem for s in SOURCES]
            subprocess.run(
                [sys.executable, "-c", "import sys; sys.path[:0] = ['.']; [__import__(m) for m in sys.argv[1:]]", *modules],
                cwd=d, env=env, check=True, capture_output=True, text=True,
            )
            out = subprocess.run(
                [sys.executable, "audit_report.py", "finish", "--help"],
                cwd=d, env=env, check=True, capture_output=True, text=True,
            )
            self.assertIn("--dry-run", out.stdout)

    def test_a_dry_run_finish_completes_from_one_directory(self):
        # A real finish on the --dry-run path, which reaches the lazy imports
        # an import or --help never does: a document from the platform tests'
        # own builder, so it's one finish accepts.
        sys.path.insert(0, str(REPO_ROOT / "agents/platform/skills/fleet-audit/scripts"))
        try:
            import test_audit_report  # noqa: PLC0415
        finally:
            sys.path.pop(0)
        document = test_audit_report.make_doc()
        with tempfile.TemporaryDirectory() as tmp:
            d = scripts_dir(tmp)
            for src in SOURCES:
                shutil.copy(REPO_ROOT / src, d / Path(src).name)
            findings = Path(tmp) / "findings.json"
            findings.write_text(json.dumps(document))
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "PYTHONDONTWRITEBYTECODE": "1",
                   "FLEET_AUDIT_SCRATCH_DIR": str(Path(tmp) / "scratch"), "FLEET_AUDIT_REPORTS_DIR": str(Path(tmp) / "reports")}
            run = subprocess.run(
                [sys.executable, "audit_report.py", "finish", "--audit", test_audit_report.AUDIT,
                 "--findings-file", str(findings), "--no-collector-manifest", "test", "--dry-run"],
                cwd=d, env=env, capture_output=True, text=True,
            )
            self.assertNotIn("ModuleNotFoundError", run.stderr)
            self.assertEqual(run.returncode, 0, run.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
