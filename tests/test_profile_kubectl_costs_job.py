"""scripts/dev/profile_kubectl_costs_job.sh against a stub kubectl: operator prose stays out of the
Job's grammar, a failed poll is "not yet", and a table not yet copied keeps its namespace."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "dev" / "profile_kubectl_costs_job.sh"
REFERENCE = "kwok-01's fleet: $HOME 104,944 Pods"
TABLE = {"tiers": {"t": {"commands": {}, "other": {"peak_mib": 256}}}}

# A kubectl that records every call, saves each applied manifest, fails the first
# `get job` poll, and answers `logs` per LOGS_MODE (ok, or fail every time).
STUB = r"""#!/usr/bin/env bash
dir=$(dirname "$0")
echo "$*" >> "$dir/calls"
args=" $* "
case "$args" in
  *" apply -f - "*) cat >> "$dir/applied.yaml"; echo "---" >> "$dir/applied.yaml" ;;
  *" get job/"*)
    if [ ! -e "$dir/polled" ]; then touch "$dir/polled"; exit 1; fi
    case "$args" in *succeeded*) echo 1 ;; esac ;;
  *" logs job/"*)
    [ "$LOGS_MODE" = fail ] && exit 1
    echo progress; echo ---BEGIN-TABLE---; cat "$dir/table.json"; echo ---END-TABLE--- ;;
esac
exit 0
"""


class JobScriptTest(unittest.TestCase):
    def run_script(self, logs_mode):
        tmp = Path(tempfile.mkdtemp())
        stub = tmp / "kubectl"
        stub.write_text(STUB)
        stub.chmod(0o755)
        (tmp / "table.json").write_text(json.dumps(TABLE) + "\n")  # as the profiler writes it
        repo = tmp / "repo"
        for rel in ("scripts/dev/profile_kubectl_costs_job.sh", "scripts/dev/profile_kubectl_costs.py",
                    "deploy/sandbox/kubectl-admit.py", "deploy/sandbox/kubectl-costs.json",
                    "deploy/sandbox/Dockerfile"):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_bytes((REPO / rel).read_bytes())
        env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", LOGS_MODE=logs_mode)
        script = repo / "scripts/dev/profile_kubectl_costs_job.sh"
        script.write_text(script.read_text().replace("POLL_SECONDS=15", "POLL_SECONDS=0"))
        result = subprocess.run(["bash", str(script), "ctx", "t", REFERENCE], env=env, capture_output=True,
                                text=True, timeout=60, check=False)
        return tmp, repo, result

    def test_the_reference_reaches_the_job_as_data_and_a_failed_poll_is_not_yet(self):
        tmp, repo, result = self.run_script("ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        job = next(d for d in yaml.safe_load_all((tmp / "applied.yaml").read_text()) if d and d["kind"] == "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual({e["name"]: e["value"] for e in container["env"]},
                         {"PROFILE_TIER": "t", "PROFILE_REFERENCE": REFERENCE})
        self.assertNotIn(REFERENCE, container["args"][0])
        self.assertEqual(json.loads((repo / "deploy/sandbox/kubectl-costs.json").read_text()), TABLE)
        self.assertIn("delete namespace", (tmp / "calls").read_text())

    def test_a_table_not_yet_copied_keeps_its_namespace(self):
        tmp, _repo, result = self.run_script("fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("kept namespace kubectl-cost-profile-", result.stderr)
        self.assertNotIn("delete namespace", (tmp / "calls").read_text())


if __name__ == "__main__":
    unittest.main()
