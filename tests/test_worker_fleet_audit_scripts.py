"""The fleet-audit report scripts in the worker image (#5623).

a2a/Dockerfile.worker copies the fleet-audit AgentProfile's report scripts into
/opt/fleet-audit/scripts from their sources at build time, flat, so a script's
own directory is where Python finds the modules it imports. This holds the COPY
to exactly that set of sources, and checks the set imports from one directory
alone, as the image's build-time check does, without building the image.
"""

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
DEST = "/opt/fleet-audit/scripts/"

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


def copy_sources(dockerfile: str) -> list[str]:
    """The sources of the COPY whose destination is DEST."""
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    for line in joined.splitlines():
        words = line.split()
        if words[:1] == ["COPY"] and words[-1] == DEST:
            return [w for w in words[1:-1] if not w.startswith("--")]
    return []


class WorkerFleetAuditScriptsTest(unittest.TestCase):
    def test_the_copy_names_exactly_the_closure(self):
        self.assertEqual(sorted(copy_sources(WORKER_DOCKERFILE.read_text())), sorted(SOURCES))

    def test_the_closure_imports_from_one_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            for src in SOURCES:
                shutil.copy(REPO_ROOT / src, Path(tmp) / Path(src).name)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "PYTHONDONTWRITEBYTECODE": "1"}
            modules = [Path(s).stem for s in SOURCES]
            subprocess.run(
                [sys.executable, "-c", "import sys; sys.path[:0] = ['.']; [__import__(m) for m in sys.argv[1:]]", *modules],
                cwd=tmp, env=env, check=True, capture_output=True, text=True,
            )
            out = subprocess.run(
                [sys.executable, "audit_report.py", "finish", "--help"],
                cwd=tmp, env=env, check=True, capture_output=True, text=True,
            )
            self.assertIn("--dry-run", out.stdout)


if __name__ == "__main__":
    unittest.main()
