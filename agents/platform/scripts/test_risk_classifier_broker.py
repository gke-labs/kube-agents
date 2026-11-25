"""End-to-end broker, bare-git, and autonomy-dial integration tests for PR risk classification."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import automerge_eligibility
import risk_classifier
import vcs_broker
from providers import NoCredential
from providers.base import BROKER_VERBS, LIBRARY_VERBS
from workspace_paths import WorkspaceError

DEPLOY_V1 = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: shop
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: app
          image: gcr.io/acme/web:v1.0.0
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
"""

NEW_DEPLOY = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
  namespace: shop
spec:
  replicas: 2
  selector:
    matchLabels:
      app: worker
  template:
    metadata:
      labels:
        app: worker
    spec:
      containers:
        - name: worker
          image: gcr.io/acme/worker:v1.0.0
"""

AUTOMATION_LOW_STAGING_MEDIUM_DEV = """\
version: 1
automerge:
  allow:
    - max_risk: low
      environments: [staging]
    - max_risk: medium
      environments: [dev]
  deny:
    - fix: "*"
      environments: [prod]
"""


class _FakeForge:
    """Minimal in-memory forge backed by a real local bare git remote."""

    name = "github"
    hosts = ("github.com",)
    labels = True
    text_sets_labels = False
    credential = NoCredential()
    verbs = frozenset(
        {"proposal-view", "proposal-list", "proposal-commits", "proposal-create", "proposal-update", "label-ensure"}
    )

    def __init__(self, remote_url: str, viewer: str = "kube-agents-bot") -> None:
        self._url = remote_url
        self._viewer = viewer
        self.proposals: dict[int, dict[str, Any]] = {}
        self.commits: dict[int, list[dict[str, Any]]] = {}
        self.ensured_labels: list[str] = []
        self.label_adds: list[tuple[int, list[str]]] = []
        self.label_removes: list[tuple[int, list[str]]] = []
        self.on_label_add: Any = None

    def clone_url(self, repo: str) -> str:
        return self._url

    def whoami(self, api: Any) -> str:
        return self._viewer

    def refuse_labels(self, verb: str, payload: dict[str, Any]) -> None:
        return None

    def proposal_view(self, api: Any, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        num = int(payload["number"])
        p = dict(self.proposals[num])
        p["labels"] = list(p.get("labels") or [])
        out: dict[str, Any] = {"proposal": p}
        if payload.get("diff") and "_diff" in p:
            out["diff"] = p["_diff"]
        return out

    def proposal_commits(self, api: Any, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        num = int(payload["number"])
        if num in self.commits:
            return {"commits": list(self.commits[num])}
        p = self.proposals[num]
        return {"commits": [{"sha": p.get("sourceRevision") or "a" * 40}]}

    def proposal_list(self, api: Any, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        src = payload.get("source")
        state = payload.get("state")
        out = []
        for p in self.proposals.values():
            if src and p.get("source") != src:
                continue
            if state and state != "all" and p.get("state") != state:
                continue
            cp = dict(p)
            cp["labels"] = list(cp.get("labels") or [])
            out.append(cp)
        return {"proposals": out}

    def proposal_create(self, api: Any, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        num = max(self.proposals.keys(), default=0) + 1
        # Resolve current source branch tip from remote
        sha = subprocess.check_output(
            ["git", "--git-dir=.", "rev-parse", f"refs/heads/{payload['source']}"],
            cwd=self._url,
            text=True,
        ).strip()
        prop = {
            "number": num,
            "state": "open",
            "author": self._viewer,
            "source": payload["source"],
            "sourceRepo": repo,
            "sourceRevision": sha,
            "target": payload["target"],
            "labels": [],
            "url": f"https://github.com/{repo}/pull/{num}",
            "title": payload.get("title", "test"),
        }
        self.proposals[num] = prop
        return {"proposal": dict(prop)}

    def label_ensure(self, api: Any, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensured_labels.append(str(payload["name"]))
        return {"label": payload["name"]}

    def proposal_update(self, api: Any, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        num = int(payload["number"])
        prop = self.proposals[num]
        labels = list(prop.get("labels") or [])
        if payload.get("labelsAdd"):
            add = list(payload["labelsAdd"])
            self.label_adds.append((num, add))
            for lbl in add:
                if lbl not in labels:
                    labels.append(lbl)
            prop["labels"] = labels
            if self.on_label_add is not None:
                self.on_label_add(prop)
        if payload.get("labelsRemove"):
            rem = list(payload["labelsRemove"])
            self.label_removes.append((num, rem))
            prop["labels"] = [lbl for lbl in labels if lbl not in rem]
        return {"proposal": dict(prop)}


def _local_git_runner(argv: list[str], cwd: Path, check: bool = True, config: tuple = (), **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=check)


class RiskBrokerIntegrationTests(unittest.TestCase):
    """End-to-end broker tests backed by a real local bare git remote."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="risk-broker-test-")
        self.root = Path(self._tmp.name)
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"
        self.scratch = self.root / "scratch"
        self.remote.mkdir()
        self.work.mkdir()
        self.scratch.mkdir()

        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.email", "bot@example.com"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.name", "Bot"], cwd=self.work, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.work, check=True)

        (self.work / ".kube-agents").mkdir()
        (self.work / ".kube-agents" / "automation.yaml").write_text(AUTOMATION_LOW_STAGING_MEDIUM_DEV, encoding="utf-8")
        (self.work / "clusters" / "dev-c1").mkdir(parents=True)
        (self.work / "clusters" / "dev-c1" / "web.yaml").write_text(DEPLOY_V1, encoding="utf-8")
        (self.work / "clusters" / "staging-c1").mkdir(parents=True)
        (self.work / "clusters" / "staging-c1" / "web.yaml").write_text(DEPLOY_V1, encoding="utf-8")
        (self.work / "clusters" / "prod-c1").mkdir(parents=True)
        (self.work / "clusters" / "prod-c1" / "web.yaml").write_text(DEPLOY_V1, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.work, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=self.work, check=True)
        self.main_sha = self._rev("HEAD")

        self.forge = _FakeForge(str(self.remote))
        self.clusters_list = [
            {"name": "dev-c1", "location": "us-east4", "resourceLabels": {"environment": "dev"}},
            {"name": "staging-c1", "location": "us-east4", "resourceLabels": {"environment": "staging"}},
            {"name": "prod-c1", "location": "us-east4", "resourceLabels": {"environment": "prod"}},
        ]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rev(self, ref: str) -> str:
        return subprocess.check_output(["git", "rev-parse", ref], cwd=self.work, text=True).strip()

    def _push_branch(self, branch: str, edits: dict[str, str | None], *, base: str = "main") -> str:
        subprocess.run(["git", "checkout", "-q", "-B", branch, base], cwd=self.work, check=True)
        for rel, content in edits.items():
            target = self.work / rel
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.work, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"change on {branch}"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "-f", "origin", branch], cwd=self.work, check=True)
        sha = self._rev("HEAD")
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.work, check=True)
        return sha

    def _cluster_read(self, argv: list[str], context: str) -> tuple[int, str, str]:
        if "describe" in argv:
            name = argv[argv.index("describe") + 1]
            for c in self.clusters_list:
                if c["name"] == name:
                    return 0, json.dumps({"resourceLabels": c["resourceLabels"]}), ""
            return 1, "", "not found"
        if "list" in argv:
            filt = next((a for a in argv if a.startswith("--filter=name=")), "")
            want = filt.split("=", 2)[-1] if filt else ""
            matches = [c for c in self.clusters_list if c["name"] == want]
            return 0, json.dumps(matches), ""
        return 1, "", "unsupported"

    def _make_broker(self, *, switch: str = "on", auto_label_risk: bool = False) -> vcs_broker.VcsBroker:
        broker = vcs_broker.VcsBroker(
            self.scratch,
            git_runner=_local_git_runner,
            cluster_read=self._cluster_read,
            automerge_switch=lambda: switch,
            auto_label_risk=auto_label_risk,
            base_branch="main",
        )
        class _StubTransport:
            def ensure(self) -> None:
                return None

            def whoami(self) -> str:
                return "kube-agents-bot"

        bound = vcs_broker.Binding(
            self.forge,
            "acme/infra",
            lambda: _StubTransport(),
            broker._git_with(lambda: (), False),
        )
        broker._bind = lambda payload: bound  # type: ignore[method-assign]
        broker._snapshot_git = lambda b: (vcs_broker.ROLE_MANAGED, b.git)  # type: ignore[method-assign]
        return broker

    def test_verb_registration_and_read_only_boundary(self) -> None:
        self.assertIn("proposal-classify-risk", vcs_broker.VCS_VERBS)
        self.assertIn("proposal-classify-risk", BROKER_VERBS)
        self.assertIn("proposal-classify-risk", LIBRARY_VERBS)
        self.assertNotIn("proposal-classify-risk", vcs_broker.WRITE_VERBS)
        self.assertNotIn("proposal-classify-risk", vcs_broker.PROPOSE_VERBS)

        self.assertIn("proposal-risk-label", vcs_broker.VCS_VERBS)
        self.assertIn("proposal-risk-label", vcs_broker.WRITE_VERBS)
        self.assertIn("proposal-risk-label", vcs_broker.PROPOSE_VERBS)

    def test_reserved_risk_labels_cannot_be_forged(self) -> None:
        for lbl in risk_classifier.RISK_LABELS:
            for verb, payload in (
                ("proposal-update", {"number": 1, "labelsAdd": [lbl]}),
                ("proposal-update", {"number": 1, "labelsAdd": [lbl.upper()]}),
                ("proposal-update", {"number": 1, "labels": [f"ok,{lbl}"]}),
                ("label-ensure", {"name": lbl}),
            ):
                refusal = automerge_eligibility.reserved_label_refusal(verb, payload)
                self.assertIsNotNone(refusal, f"{verb} {payload} should be refused")

    def test_autonomy_dial_levels_low_vs_medium_and_prod_deny_with_auto_cluster_resolution(self) -> None:
        # 1. Low-risk bump in staging -> allowed by `max_risk: low` in staging!
        sha_stg = self._push_branch("platform-agent/stg-bump", {"clusters/staging-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")})
        self.forge.proposals[1] = {
            "number": 1, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/stg-bump", "sourceRepo": "acme/infra",
            "sourceRevision": sha_stg, "target": "main", "labels": [],
        }
        # 2. Medium-risk new Deployment in dev -> allowed by `max_risk: medium` in dev!
        sha_dev = self._push_branch("platform-agent/dev-new", {"clusters/dev-c1/worker.yaml": NEW_DEPLOY})
        self.forge.proposals[2] = {
            "number": 2, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/dev-new", "sourceRepo": "acme/infra",
            "sourceRevision": sha_dev, "target": "main", "labels": [],
        }
        # 3. Medium-risk new Deployment in staging -> refused (staging only allows `max_risk: low`)!
        sha_stg_med = self._push_branch("platform-agent/stg-new", {"clusters/staging-c1/worker.yaml": NEW_DEPLOY})
        self.forge.proposals[3] = {
            "number": 3, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/stg-new", "sourceRepo": "acme/infra",
            "sourceRevision": sha_stg_med, "target": "main", "labels": [],
        }
        # 4. Low-risk bump in prod -> denied by `fix: "*"` deny in prod!
        sha_prod = self._push_branch("platform-agent/prod-bump", {"clusters/prod-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")})
        self.forge.proposals[4] = {
            "number": 4, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/prod-bump", "sourceRepo": "acme/infra",
            "sourceRevision": sha_prod, "target": "main", "labels": [],
        }

        broker = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            # Notice NO `cluster` arg passed by caller -- broker resolves cluster from path!
            r1 = broker.proposal_risk_label({"repository": "acme/infra", "number": 1})
            self.assertEqual(r1["risk_level"], "low")
            self.assertTrue(r1["labelled"])
            self.assertEqual(r1["label"], automerge_eligibility.AUTOMERGE_LABEL)
            self.assertEqual(sorted(self.forge.proposals[1]["labels"]), ["kube-agents:auto-merge", "risk:low"])

            r2 = broker.proposal_risk_label({"repository": "acme/infra", "number": 2})
            self.assertEqual(r2["risk_level"], "medium")
            self.assertTrue(r2["labelled"])
            self.assertEqual(sorted(self.forge.proposals[2]["labels"]), ["kube-agents:auto-merge", "risk:medium"])

            r3 = broker.proposal_risk_label({"repository": "acme/infra", "number": 3})
            self.assertEqual(r3["risk_level"], "medium")
            self.assertFalse(r3["labelled"])
            self.assertEqual(self.forge.proposals[3]["labels"], ["risk:medium"])

            r4 = broker.proposal_risk_label({"repository": "acme/infra", "number": 4})
            self.assertFalse(r4["labelled"])
            self.assertIn("risk:medium", self.forge.proposals[4]["labels"])  # prod path escalates low -> medium!

        # Install ceiling KUBEAGENTS_AUTOMERGE_MAX_RISK=low blocks medium in dev
        with mock.patch.dict(os.environ, {"KUBEAGENTS_AUTOMERGE_MAX_RISK": "low"}):
            with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
                r2_clamped = broker.proposal_risk_label({"repository": "acme/infra", "number": 2})
                self.assertFalse(r2_clamped["labelled"])
                self.assertEqual(self.forge.proposals[2]["labels"], ["risk:medium"])

        # Dial below Auto-merge blocks `on` label, while `practice` still applies `would-auto-merge`
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=False):
            r1_dial = broker.proposal_risk_label({"repository": "acme/infra", "number": 1})
            self.assertFalse(r1_dial["labelled"])
            self.assertEqual(self.forge.proposals[1]["labels"], ["risk:low"])

            broker_prac = self._make_broker(switch="practice")
            r1_prac = broker_prac.proposal_risk_label({"repository": "acme/infra", "number": 1})
            self.assertTrue(r1_prac["labelled"])
            self.assertEqual(r1_prac["label"], automerge_eligibility.PRACTICE_LABEL)

    def test_merged_pr_and_deleted_branch_classify_accurate_historical_delta(self) -> None:
        # Commit PR 10: bump dev-c1 replicas 3 -> 4, merge it to main, delete its branch,
        # then commit a LATER change on main scaling replicas 4 -> 11.
        pr_sha = self._push_branch("platform-agent/old-bump", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")})
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.work, check=True)
        subprocess.run(["git", "merge", "-q", "--no-ff", pr_sha, "-m", "Merge PR #10"], cwd=self.work, check=True)
        merge_sha = self._rev("HEAD")
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.work, check=True)
        # Delete the remote branch (GitHub delete-branch-on-merge)
        subprocess.run(["git", "push", "-q", "origin", "--delete", "platform-agent/old-bump"], cwd=self.work, check=True)
        # Advance main further to replicas: 11!
        (self.work / "clusters" / "dev-c1" / "web.yaml").write_text(DEPLOY_V1.replace("replicas: 3", "replicas: 11"), encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.work, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "later bump to 11"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.work, check=True)

        self.forge.proposals[10] = {
            "number": 10, "state": "merged", "author": "kube-agents-bot",
            "source": "platform-agent/old-bump", "sourceRepo": "acme/infra",
            "sourceRevision": pr_sha, "mergeCommit": merge_sha, "target": "main", "labels": [],
        }
        broker = self._make_broker(switch="off")
        res = broker.proposal_classify_risk({"repository": "acme/infra", "number": 10})
        # Must accurately see 3 -> 4 (`low`), NOT 11 -> 4 steep scale-down!
        self.assertEqual(res["risk_level"], "low")
        self.assertEqual(res["destructiveness"], "non_destructive")

    def test_stale_base_same_file_modification_withholds_automerge(self) -> None:
        pr_sha = self._push_branch("platform-agent/stale-pr", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")})
        # Now advance main on the SAME file!
        (self.work / "clusters" / "dev-c1" / "web.yaml").write_text(DEPLOY_V1.replace("v1.0.0", "v1.0.1"), encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.work, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "main moved same file"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.work, check=True)

        self.forge.proposals[11] = {
            "number": 11, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/stale-pr", "sourceRepo": "acme/infra",
            "sourceRevision": pr_sha, "target": "main", "labels": [],
        }
        broker = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            res = broker.proposal_risk_label({"repository": "acme/infra", "number": 11})
        self.assertEqual(res["risk_level"], "low")
        self.assertTrue(res["stale_base"])
        self.assertFalse(res["labelled"])
        self.assertIn("rebase", res["reason"])
        self.assertEqual(self.forge.proposals[11]["labels"], ["risk:low"])

    def test_fleet_audit_proposal_practice_label_preserves_risk_automerge_label(self) -> None:
        sha = self._push_branch("platform-agent/dev-bump", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")})
        self.forge.proposals[12] = {
            "number": 12, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/dev-bump", "sourceRepo": "acme/infra",
            "sourceRevision": sha, "target": "main",
            "labels": ["risk:low", "kube-agents:auto-merge"],
        }
        broker = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            res = broker.proposal_practice_label({"repository": "acme/infra", "number": 12})
        self.assertTrue(res["labelled"])
        self.assertIn("kube-agents:auto-merge", self.forge.proposals[12]["labels"])

    def test_auto_label_risk_on_proposal_create_and_cluster_spoof_refusal(self) -> None:
        sha = self._push_branch("platform-agent/auto-open", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")})
        broker = self._make_broker(switch="on", auto_label_risk=True)
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            created = broker.proposal_create(
                {"repository": "acme/infra", "source": "platform-agent/auto-open", "target": "main", "title": "Bump"}
            )
        num = created["proposal"]["number"]
        self.assertEqual(sorted(self.forge.proposals[num]["labels"]), ["kube-agents:auto-merge", "risk:low"])

        # Spoofing dev-c1 cluster coordinate for a staging-c1 PR is refused
        sha_stg = self._push_branch("platform-agent/spoof", {"clusters/staging-c1/worker.yaml": NEW_DEPLOY})
        self.forge.proposals[20] = {
            "number": 20, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/spoof", "sourceRepo": "acme/infra",
            "sourceRevision": sha_stg, "target": "main", "labels": [],
        }
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            r_spoof = broker.proposal_risk_label(
                {"repository": "acme/infra", "number": 20, "cluster": "my-gcp-project/us-east4/dev-c1"}
            )
        self.assertFalse(r_spoof["labelled"])
        self.assertIn("does not match", r_spoof["reason"])

    def test_fork_pr_returns_unknown_and_toctou_same_sha_merge_preserves_labels(self) -> None:
        self.forge.proposals[30] = {
            "number": 30, "state": "open", "author": "stranger",
            "source": "patch-1", "sourceRepo": "evil/infra",
            "sourceRevision": "a" * 40, "target": "main", "labels": [],
        }
        broker = self._make_broker(switch="on")
        r_fork = broker.proposal_classify_risk({"repository": "acme/infra", "number": 30})
        self.assertEqual(r_fork["risk_level"], "unknown")
        self.assertFalse(r_fork["supported"])

        # Same-SHA immediate auto-merge on label apply preserves labels
        sha = self._push_branch("platform-agent/instant-merge", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")})
        self.forge.proposals[31] = {
            "number": 31, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/instant-merge", "sourceRepo": "acme/infra",
            "sourceRevision": sha, "target": "main", "labels": [],
        }
        self.forge.on_label_add = lambda prop: prop.update({"state": "merged"})
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            r_merge = broker.proposal_risk_label({"repository": "acme/infra", "number": 31})
        self.assertTrue(r_merge["labelled"])
        self.assertEqual(sorted(self.forge.proposals[31]["labels"]), ["kube-agents:auto-merge", "risk:low"])

    def test_empty_source_repo_fork_pr_refused_as_unknown(self) -> None:
        """GitLab fork MRs and deleted GitHub forks set sourceRepo="" and must fail closed."""
        self.forge.proposals[40] = {
            "number": 40, "state": "open", "author": "stranger",
            "source": "main", "sourceRepo": "",
            "sourceRevision": self.main_sha, "target": "main", "labels": [],
        }
        broker = self._make_broker(switch="on")
        res = broker.proposal_classify_risk({"repository": "acme/infra", "number": 40})
        self.assertEqual(res["risk_level"], "unknown")
        self.assertFalse(res["supported"])
        self.assertEqual(res["reasons"][0]["code"], "FORK_PROPOSAL_UNSUPPORTED")

    def test_merged_ff_without_merge_commit_and_multi_commit_single_parent_fail_closed(self) -> None:
        # 1. Fast-forward merge with empty mergeCommit must NOT fall into open-PR branch
        self.forge.proposals[41] = {
            "number": 41, "state": "merged", "author": "kube-agents-bot",
            "source": "platform-agent/ff", "sourceRepo": "acme/infra",
            "sourceRevision": self.main_sha, "target": "main",
            "mergeCommit": "", "labels": [],
        }
        broker = self._make_broker(switch="off")
        r_ff = broker.proposal_classify_risk({"repository": "acme/infra", "number": 41})
        self.assertEqual(r_ff["risk_level"], "unknown")
        self.assertEqual(r_ff["reasons"][0]["code"], "MERGED_PR_NO_MERGE_COMMIT")

        # 2. Multi-commit single-parent mergeCommit (rebase-and-merge) fails closed so commit 1 is never masked
        sha_reb = self._push_branch(
            "main",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
            base="main",
        )
        self.forge.proposals[42] = {
            "number": 42, "state": "merged", "author": "kube-agents-bot",
            "source": "platform-agent/rebased", "sourceRepo": "acme/infra",
            "sourceRevision": sha_reb, "target": "main",
            "mergeCommit": sha_reb, "labels": [],
        }
        self.forge.commits[42] = [{"sha": "1" * 40}, {"sha": sha_reb}]
        r_reb = broker.proposal_classify_risk({"repository": "acme/infra", "number": 42})
        self.assertEqual(r_reb["risk_level"], "unknown")
        self.assertEqual(r_reb["reasons"][0]["code"], "MERGED_PR_MULTI_COMMIT_SINGLE_PARENT")

    def test_no_pdb_denied_never_bypassed_by_max_risk_medium(self) -> None:
        """A `deny: [{fix: no-pdb}]` rule or failed live check can NEVER be bypassed via `max_risk: medium`."""
        rules_deny_nopdb = (
            "version: 1\nautomerge:\n  allow:\n"
            "    - max_risk: medium\n      environments: [dev]\n"
            "  deny:\n    - fix: no-pdb\n      environments: [dev]\n"
        )
        self._push_branch("main", {".kube-agents/automation.yaml": rules_deny_nopdb}, base="main")
        pdb_yaml = (
            "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n"
            "  name: web-pdb\n  namespace: shop\nspec:\n  minAvailable: 1\n"
            "  selector:\n    matchLabels:\n      app: web\n"
        )
        sha = self._push_branch("platform-agent/add-pdb", {"clusters/dev-c1/web-pdb.yaml": pdb_yaml}, base="main")
        broker = self._make_broker(switch="on", auto_label_risk=True)
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            created = broker.proposal_create(
                {
                    "repository": "acme/infra",
                    "source": "platform-agent/add-pdb",
                    "target": "main",
                    "title": "Add PDB",
                    "body": "offline pdb",
                }
            )
            num = created["proposal"]["number"]
            # Must receive risk:medium, MUST NOT receive kube-agents:auto-merge!
            self.assertEqual(self.forge.proposals[num]["labels"], ["risk:medium"])
            # proposal_practice_label must also NOT apply kube-agents:auto-merge!
            prac = broker.proposal_practice_label({"repository": "acme/infra", "number": num})
            self.assertFalse(prac["labelled"])
            self.assertNotIn("kube-agents:auto-merge", self.forge.proposals[num]["labels"])

    def test_malicious_cluster_dir_name_refused_before_gcloud_filter(self) -> None:
        called = []

        def spy_cluster_read(argv: list[str], context: str) -> tuple[int, str, str]:
            called.append(argv)
            return 0, "[]", ""

        sha = self._push_branch(
            "platform-agent/bad-cluster",
            {"clusters/dev OR name=prod/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        self.forge.proposals[45] = {
            "number": 45, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/bad-cluster", "sourceRepo": "acme/infra",
            "sourceRevision": sha, "target": "main", "labels": [],
        }
        broker = self._make_broker(switch="on")
        broker._cluster_read = spy_cluster_read
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            res = broker.proposal_risk_label({"repository": "acme/infra", "number": 45})
        self.assertFalse(res["labelled"])
        self.assertEqual(called, [])

        # Also verify uppercase/non-GKE cluster directory name is refused before gcloud
        sha2 = self._push_branch(
            "platform-agent/bad-cluster-caps",
            {"clusters/BAD_CLUSTER/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        self.forge.proposals[46] = {
            "number": 46, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/bad-cluster-caps", "sourceRepo": "acme/infra",
            "sourceRevision": sha2, "target": "main", "labels": [],
        }
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            res2 = broker.proposal_risk_label({"repository": "acme/infra", "number": 46})
        self.assertFalse(res2["labelled"])
        self.assertIn("invalid cluster directory name", res2["reason"])
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
