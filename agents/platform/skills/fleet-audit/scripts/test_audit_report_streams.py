"""Per-stream tests of `audit_report.py finish`, one sample finding on every audit stream.

Run:
  python3 -m unittest discover -s agents/platform/skills/fleet-audit/scripts \
      -p 'test_audit_report_streams.py' -v

No test here needs a cluster or a forge. The dry run starts the script as a child
process and reads the ledger it prints. The lifecycle test calls `main` in this
process with the forge verbs and `run_cmd` replaced by recorders. A third test
holds `AUDIT_STREAMS` to the streams in `audit_report.AUDITS`.

`finish` runs here with no `start` record before it. A finding whose check is in
the stream's `declarable` set then has no declared-intent search on record, and
`finish` withholds it. `_sample_cluster_checks` picks a check outside that set.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import audit_report  # noqa: E402
import gitops_workspace  # noqa: E402
import no_live_google_auth  # noqa: E402
import no_live_kubectl  # noqa: E402
import vcs_client  # noqa: E402

_AUDIT_REPORT_SCRIPT = Path(audit_report.__file__).resolve()

# The streams these tests run, with the title each ledger issue carries.
# `test_the_list_names_every_stream` holds the list to `audit_report.AUDITS`.
AUDIT_STREAMS: List[Tuple[str, str]] = [
    ("compliance-audit", "Security & RBAC Posture Audit"),
    ("security-patch-orchestrator", "Upgrade & Patch Readiness Audit"),
    ("obtainability-audit", "Workload Reliability Audit"),
    ("fleet-wide-cost-analysis", "Fleet Waste Audit"),
    ("fleet-consistency-drift", "Fleet Consistency Drift Audit"),
    ("ai-security-audit", "AI Workload Security Audit"),
    ("stockout-prevention", "Fleet Stockout Prevention & Capacity Audit"),
    ("gcp-networking-fabric-audit", "GCP Networking Fabric & VPC IPAM Audit"),
    ("gce-compute-fleet-audit", "GCE Compute Engine and MIG Fleet Audit"),
    ("upgrade-retrospective", "Upgrade Retrospective"),
]

_CLUSTER = "test-cluster"
_PROJECT = "test-project"
_LOCATION = "us-east4"
_REPOSITORY = "test-org-kube-agent/agents-repo"
_LEDGER_ISSUE = 42
_LEDGER_BODY = "<!-- audit-findings: [] -->"
_NA_REASON = "GKE Autopilot: Google owns this resource; check is not applicable on Autopilot clusters."


def setUpModule():
    # Neither test reaches a cluster or loads google.auth, in this process or in
    # the child the dry run starts.
    global _NO_KUBECTL, _NO_GOOGLE_AUTH
    _NO_KUBECTL = no_live_kubectl.install()
    _NO_GOOGLE_AUTH = no_live_google_auth.install()


def tearDownModule():
    try:
        _NO_GOOGLE_AUTH.uninstall()
    finally:
        _NO_KUBECTL.uninstall()


def _collector_waiver(audit_id: str) -> List[str]:
    """`finish` on a collector stream requires a manifest or a waiver; these runs have no collector."""
    if audit_id not in audit_report.COLLECTOR_AUDITS:
        return []
    return ["--no-collector-manifest", "test fixture document; no collector ran"]


def _sample_cluster_checks(spec) -> Tuple[str, str]:
    """Return `(valid_check, na_check)` from `spec` for a cluster-scoped `finish` fixture.

    `valid_check` is the first cluster-scoped check not in `spec.declarable` (the
    module docstring says why). `na_check` is a distinct cluster-scoped check:
    `finish` rejects a document that names one check in both `checks_run` and
    `checks_not_applicable`.
    """
    cluster_checks = next(
        (checks for kind, checks in spec.scopes if kind == "cluster"),
        spec.checks,
    )
    valid_check = next(
        (c for c in cluster_checks if c not in spec.declarable),
        cluster_checks[0],
    )
    na_check = next(c for c in cluster_checks if c != valid_check)
    return valid_check, na_check


def _sample_finding(valid_check: str, human_name: str) -> dict:
    return {
        "check": valid_check,
        "severity": "major",
        "title": f"Sample finding for {human_name}",
        "cluster": _CLUSTER,
        "namespace": "default",
        "object": "Deployment/sample-app",
        "evidence": {
            "command": f"kubectl --context={_CLUSTER} get pods -A",
            "excerpt": "sample evidence excerpt",
        },
        "impact": "Sample impact description for audit finding.",
        "recommendation": {
            "action": "Take recommended remediation step.",
            "rationale": "Rationale explaining why this change was chosen.",
            "risk": "Risk evaluation for this specific recommendation.",
        },
        "remediation": {
            "kind": "manual",
            "note": f"kubectl --context={_CLUSTER} edit deployment/sample-app",
        },
    }


def _sample_document(audit_id: str, human_name: str, valid_check: str, checks_not_applicable=None) -> dict:
    cluster = {
        "name": _CLUSTER,
        "location": _LOCATION,
        "project": _PROJECT,
        "checks_run": [
            {
                "check": valid_check,
                "command": f"kubectl --context={_CLUSTER} get pods -A",
            }
        ],
    }
    if checks_not_applicable is not None:
        cluster["checks_not_applicable"] = checks_not_applicable
    return {
        "audit": audit_id,
        "scope": {"clusters": [cluster], "skipped": []},
        "findings": [_sample_finding(valid_check, human_name)],
    }


class TestFinishOnEveryStream(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_the_list_names_every_stream(self):
        """A stream added to `audit_report.AUDITS` and not to `AUDIT_STREAMS` would run in neither test."""
        self.assertEqual(sorted(audit_id for audit_id, _ in AUDIT_STREAMS), sorted(audit_report.AUDITS))

    def test_audit_report_ledger_dryrun_all_streams(self):
        """`finish --dry-run` accepts the sample document and prints its ledger, on every stream.

        Covers the schema, the check roster, and the ledger text. The dry run writes
        nothing to a forge.
        """
        # The child reads its directories and the broker address from the
        # environment; none of them may come from the host that runs the test.
        # The state directory exists and holds no key, which is an install with no
        # repository registered: without it the child asks kubectl for the
        # ConfigMap, and the guard fails the module.
        state_dir = self.tmp_path / "state"
        state_dir.mkdir()
        env = {
            **os.environ,
            "CREDENTIAL_PROXY_URL": "",
            "FLEET_AUDIT_REPORT_STORE": "",
            no_live_kubectl.STATE_PATH_ENV: str(state_dir / gitops_workspace.MANAGED_REPOS_KEY),
            "FLEET_AUDIT_SCRATCH_DIR": str(self.tmp_path / "scratch"),
            "FLEET_AUDIT_GITOPS_ROOT": str(self.tmp_path / "gitops"),
            "FLEET_AUDIT_REPORTS_DIR": str(self.tmp_path / "reports"),
        }
        for audit_id, human_name in AUDIT_STREAMS:
            with self.subTest(audit=audit_id):
                self.assertIn(audit_id, audit_report.AUDITS)
                spec = audit_report.AUDITS[audit_id]
                self.assertGreaterEqual(len(spec.checks), 2)
                valid_check, na_check = _sample_cluster_checks(spec)
                document = _sample_document(
                    audit_id,
                    human_name,
                    valid_check,
                    checks_not_applicable=[{"check": na_check, "reason": _NA_REASON}],
                )
                findings_file = self.tmp_path / f"findings_{audit_id}.json"
                findings_file.write_text(json.dumps(document), encoding="utf-8")

                proc = subprocess.run(
                    [
                        sys.executable,
                        str(_AUDIT_REPORT_SCRIPT),
                        "finish",
                        f"--audit={audit_id}",
                        f"--findings-file={findings_file}",
                        "--dry-run",
                        *_collector_waiver(audit_id),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                output = f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                self.assertEqual(proc.returncode, 0, output)
                self.assertIn("Not applicable (1)", proc.stdout, output)
                self.assertIn(f"`{na_check}`", proc.stdout, output)
                self.assertIn(_NA_REASON, proc.stdout, output)
                # Absent when `finish` withheld the finding.
                self.assertIn(f"Sample finding for {human_name}", proc.stdout, output)

    def _finish_through_recorders(self, audit_id: str, human_name: str) -> List[Tuple[str, dict]]:
        """Run `finish` for one stream in this process and return the forge calls it made."""
        root = self.tmp_path / audit_id
        workspace = root / "workspace"
        (workspace / ".git").mkdir(parents=True)
        forge_calls: List[Tuple[str, dict]] = []

        def record_run_cmd(cmd, **kwargs):
            cmd_list = list(cmd)
            if cmd_list[:2] == ["git", "clone"]:
                (Path(cmd_list[-1]) / ".git").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd_list, 0, stdout="", stderr="")

        def record_forge(verb, payload, repository=None):
            forge_calls.append((verb, dict(payload)))
            if verb == "issue-list":
                url = f"https://github.com/{_REPOSITORY}/issues/{_LEDGER_ISSUE}"
                issue = {"number": _LEDGER_ISSUE, "url": url, "body": _LEDGER_BODY}
                return {"issues": [issue], "count": 1, "truncated": False}
            if verb == "issue-view":
                answer = {"issue": {"number": _LEDGER_ISSUE, "body": _LEDGER_BODY}}
                if payload.get("comments"):
                    answer.update(comments=[], commentCount=0, commentsTruncated=False)
                return answer
            if verb == "proposal-list":
                return {"proposals": [], "count": 0, "truncated": False}
            if verb == "identity":
                return {"identity": {"login": "audit-bot", "subject": "audit-bot", "canWrite": None}}
            return {}

        # What one run leaves in the module for the next: the workspace, the mode,
        # and the memoised answers about the repository.
        def forget_run_state():
            audit_report.set_workspace(None)
            audit_report.set_content_mode(False)
            audit_report._VIEWER_LOGINS.clear()
            audit_report._CAPABILITIES.clear()
            audit_report._LABELS.clear()
            audit_report._LAST_REFUSAL.clear()
            gitops_workspace.forget_base_branch()

        valid_check, _ = _sample_cluster_checks(audit_report.AUDITS[audit_id])
        findings_file = root / "findings.json"
        findings_file.write_text(
            json.dumps(_sample_document(audit_id, human_name, valid_check)), encoding="utf-8"
        )
        # An install with `_REPOSITORY` registered: `finish` takes the forge path.
        state_file = root / "state" / gitops_workspace.MANAGED_REPOS_KEY
        state_file.parent.mkdir()
        state_file.write_text(
            json.dumps([{"type": "github", "url": f"https://github.com/{_REPOSITORY}"}]), encoding="utf-8"
        )

        with contextlib.ExitStack() as stack:
            forget_run_state()
            stack.callback(forget_run_state)
            # The run takes directory mode, the file store and the repository list
            # from here, not from the host. `GITOPS_STATE_DIR` names no directory,
            # so a host with the operator's mount reads the same state file.
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "CREDENTIAL_PROXY_URL": "",
                        "FLEET_AUDIT_REPORT_STORE": "",
                        "FLEET_AUDIT_REPORTS_DIR": str(root / "reports"),
                        no_live_kubectl.STATE_PATH_ENV: str(state_file),
                    },
                )
            )
            stack.enter_context(patch.object(gitops_workspace, "GITOPS_STATE_DIR", str(root / "no-such-mount")))
            stack.enter_context(patch.object(audit_report, "GITOPS_WORKSPACE", str(root)))
            stack.enter_context(patch.object(audit_report, "SCRATCH_DIR", str(root)))
            stack.enter_context(patch.object(audit_report, "run_cmd", record_run_cmd))
            stack.enter_context(patch.object(vcs_client, "forge", record_forge))
            stack.enter_context(patch.object(audit_report, "refresh_credentials", lambda *a, **k: None))
            stack.enter_context(patch.object(audit_report, "resolve_repo", lambda *a, **k: _REPOSITORY))
            stack.enter_context(patch.object(audit_report, "repo_root", lambda: workspace))
            audit_report.set_workspace(workspace)

            # `finish` logs each step and prints its result; both are shown only
            # when the run fails.
            printed = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            logged = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            exit_code = audit_report.main(
                [
                    "finish",
                    f"--audit={audit_id}",
                    f"--findings-file={findings_file}",
                    *_collector_waiver(audit_id),
                ]
            )
        self.assertEqual(exit_code, 0, f"STDOUT:\n{printed.getvalue()}\nSTDERR:\n{logged.getvalue()}")
        return forge_calls

    def test_audit_report_github_api_lifecycle_mocked(self):
        """`finish` makes the expected forge calls, on every stream.

        1. It ensures the stream's audit label exists on the repository.
        2. It lists the ledger issues that carry that label.
        3. It updates the ledger issue it found, with the stream's title.
        4. It creates no pull request.
        """
        for audit_id, human_name in AUDIT_STREAMS:
            with self.subTest(audit=audit_id):
                forge_calls = self._finish_through_recorders(audit_id, human_name)

                def called(verb, **fields):
                    return [
                        payload
                        for seen, payload in forge_calls
                        if seen == verb
                        and all(
                            (want in payload.get(key, ()))
                            if isinstance(payload.get(key), list)
                            else str(want) in str(payload.get(key, ""))
                            for key, want in fields.items()
                        )
                    ]

                self.assertTrue(
                    called("label-ensure", name=f"audit:{audit_id}"),
                    f"no forge call ensured the 'audit:{audit_id}' label",
                )
                self.assertTrue(
                    called("issue-list", labels=f"audit:{audit_id}"),
                    f"no forge call listed the ledger issue for '{audit_id}'",
                )
                self.assertTrue(
                    called("issue-update", number=_LEDGER_ISSUE, title=f"[audit] {human_name}"),
                    f"no forge call updated issue #{_LEDGER_ISSUE} with the '{human_name}' title",
                )
                self.assertFalse(
                    called("proposal-create"),
                    "finish created a pull request for a manual-remediation finding",
                )


if __name__ == "__main__":
    unittest.main()
