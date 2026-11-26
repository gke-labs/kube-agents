"""End-to-end broker, bare-git, and autonomy-dial integration tests for PR risk classification."""

from __future__ import annotations

import base64
import concurrent.futures
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

AUTOMATION_LOW_TEST_MEDIUM_DEV = """\
version: 1
automerge:
  allow:
    - max_risk: low
      environments: [test]
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
    commit_statuses = True
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
        self.statuses: dict[tuple[str, str], tuple[str, str]] = {}
        self.status_calls: list[dict[str, Any]] = []
        self.fail_status_contexts: set[str] = set()
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
        if payload.get("diff"):
            if "_diff" in p:
                out["diff"] = p["_diff"]
            else:
                target = str(p.get("target") or "main")
                src_rev = str(p.get("sourceRevision") or "")
                remote_dir = self._url[len("file://") :] if self._url.startswith("file://") else self._url
                proc = subprocess.run(
                    ["git", "--git-dir=.", "diff", f"refs/heads/{target}...{src_rev}"],
                    cwd=remote_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                out["diff"] = proc.stdout if proc.returncode == 0 else ""
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
        if payload.get("target") is not None:
            prop["target"] = payload["target"]
        if payload.get("labelsRemove"):
            rem = list(payload["labelsRemove"])
            self.label_removes.append((num, rem))
            prop["labels"] = [lbl for lbl in labels if lbl not in rem]
        return {"proposal": dict(prop)}

    def commit_status_set(self, api: Any, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        ctx = str(payload["context"])
        if ctx in self.fail_status_contexts:
            raise RuntimeError(f"simulated status failure on {ctx}")
        rev = str(payload["revision"])
        state = str(payload["state"])
        desc = str(payload.get("description") or "")
        self.statuses[(rev, ctx)] = (state, desc)
        self.status_calls.append({"revision": rev, "state": state, "context": ctx, "description": desc})
        return {"status": {"revision": rev, "state": state, "context": ctx, "description": desc}}


_HERMETIC_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Bot",
    "GIT_AUTHOR_EMAIL": "bot@example.com",
    "GIT_COMMITTER_NAME": "Bot",
    "GIT_COMMITTER_EMAIL": "bot@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.bareRepository",
    "GIT_CONFIG_VALUE_0": "explicit",
    "KUBEAGENTS_AUTOMERGE_MAX_RISK": "medium",
}

_SCRUBBED_BROKER_ENV_KEYS = (
    "CREDENTIAL_PROXY_SCOPE_FILE",
    "CREDENTIAL_PROXY_HOLD_FILE",
    "GKE_PROJECT_ID",
    "KUBEAGENTS_AUTOMERGE_MAX_RISK",
    "KUBEAGENTS_AUTONOMY_LEVEL",
)


def _local_git_runner(argv: list[str], cwd: Path, check: bool = True, config: tuple = (), **kw) -> subprocess.CompletedProcess:
    flags = [flag for key, value in config for flag in ("-c", f"{key}={value}")]
    cmd = [argv[0], *flags, *argv[1:]] if flags else argv
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=check)


class RiskBrokerIntegrationTests(unittest.TestCase):
    """End-to-end broker tests backed by a real local bare git remote."""

    def setUp(self) -> None:
        clean_env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_BROKER_ENV_KEYS}
        clean_env.update(_HERMETIC_GIT_ENV)
        self._env_patch = mock.patch.dict(os.environ, clean_env, clear=True)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self._tmp = tempfile.TemporaryDirectory(prefix="risk-broker-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"
        self.scratch = self.root / "scratch"
        self.remote.mkdir()
        self.work.mkdir()
        self.scratch.mkdir()

        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "--git-dir=.", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=self.remote, check=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.email", "bot@example.com"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.name", "Bot"], cwd=self.work, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.work, check=True)

        (self.work / ".kube-agents").mkdir()
        (self.work / ".kube-agents" / "automation.yaml").write_text(AUTOMATION_LOW_TEST_MEDIUM_DEV, encoding="utf-8")
        (self.work / "clusters" / "dev-c1").mkdir(parents=True)
        (self.work / "clusters" / "dev-c1" / "web.yaml").write_text(DEPLOY_V1, encoding="utf-8")
        (self.work / "clusters" / "test-c1").mkdir(parents=True)
        (self.work / "clusters" / "test-c1" / "web.yaml").write_text(DEPLOY_V1, encoding="utf-8")
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
            {
                "name": "dev-c1",
                "location": "us-east4",
                "selfLink": "https://container.googleapis.com/v1/projects/my-gcp-project/locations/us-east4/clusters/dev-c1",
                "resourceLabels": {"environment": "dev"},
            },
            {
                "name": "test-c1",
                "location": "us-east4",
                "selfLink": "https://container.googleapis.com/v1/projects/my-gcp-project/locations/us-east4/clusters/test-c1",
                "resourceLabels": {"environment": "test"},
            },
            {
                "name": "staging-c1",
                "location": "us-east4",
                "selfLink": "https://container.googleapis.com/v1/projects/my-gcp-project/locations/us-east4/clusters/staging-c1",
                "resourceLabels": {"environment": "staging"},
            },
            {
                "name": "prod-c1",
                "location": "us-east4",
                "selfLink": "https://container.googleapis.com/v1/projects/my-gcp-project/locations/us-east4/clusters/prod-c1",
                "resourceLabels": {"environment": "prod"},
            },
        ]

    def tearDown(self) -> None:
        pass

    def _rev(self, ref: str) -> str:
        return subprocess.check_output(["git", "rev-parse", ref], cwd=self.work, text=True).strip()

    def _risk_label(self, broker: vcs_broker.VcsBroker, number: int, **extra: Any) -> dict[str, Any]:
        req: dict[str, Any] = {"repository": "acme/infra", "number": number}
        if "expected_head_sha" not in extra:
            req["expected_head_sha"] = self.forge.proposals[number]["sourceRevision"]
        req.update(extra)
        return broker.proposal_risk_label(req)

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
        # 1. Low-risk bump in test -> allowed by `max_risk: low` in test!
        sha_tst = self._push_branch("platform-agent/tst-bump", {"clusters/test-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")})
        self.forge.proposals[1] = {
            "number": 1, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/tst-bump", "sourceRepo": "acme/infra",
            "sourceRevision": sha_tst, "target": "main", "labels": [],
        }
        # 2. Medium-risk new Deployment in dev -> allowed by `max_risk: medium` in dev!
        sha_dev = self._push_branch("platform-agent/dev-new", {"clusters/dev-c1/worker.yaml": NEW_DEPLOY})
        self.forge.proposals[2] = {
            "number": 2, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/dev-new", "sourceRepo": "acme/infra",
            "sourceRevision": sha_dev, "target": "main", "labels": [],
        }
        # 3. Medium-risk new Deployment in test -> refused (test only allows `max_risk: low`)!
        sha_tst_med = self._push_branch("platform-agent/tst-new", {"clusters/test-c1/worker.yaml": NEW_DEPLOY})
        self.forge.proposals[3] = {
            "number": 3, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/tst-new", "sourceRepo": "acme/infra",
            "sourceRevision": sha_tst_med, "target": "main", "labels": [],
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
            r1 = self._risk_label(broker, 1)
            self.assertEqual(r1["risk_level"], "low")
            self.assertTrue(r1["labelled"])
            self.assertEqual(r1["label"], automerge_eligibility.AUTOMERGE_LABEL)
            self.assertEqual(sorted(self.forge.proposals[1]["labels"]), ["kube-agents:auto-merge", "risk:low"])
            # Commit statuses bind both risk verdict and auto-merge eligibility to head SHA + base SHA
            sha1 = self.forge.proposals[1]["sourceRevision"]
            self.assertIn((sha1, vcs_broker.RISK_VERDICT_STATUS_CONTEXT), self.forge.statuses)
            am_state, am_desc = self.forge.statuses[(sha1, vcs_broker.RISK_AUTOMERGE_STATUS_CONTEXT)]
            self.assertEqual(am_state, "success")
            self.assertIn(f"base:{self.main_sha[:12]}", am_desc)
            self.assertIn("WORKLOAD_REPLICA_BUMP", am_desc)

            r2 = self._risk_label(broker, 2)
            self.assertEqual(r2["risk_level"], "medium")
            self.assertTrue(r2["labelled"])
            self.assertEqual(sorted(self.forge.proposals[2]["labels"]), ["kube-agents:auto-merge", "risk:medium"])

            r3 = self._risk_label(broker, 3)
            self.assertEqual(r3["risk_level"], "medium")
            self.assertFalse(r3["labelled"])
            self.assertEqual(self.forge.proposals[3]["labels"], ["risk:medium"])

            r4 = self._risk_label(broker, 4)
            self.assertFalse(r4["labelled"])
            self.assertIn("risk:medium", self.forge.proposals[4]["labels"])  # prod path escalates low -> medium!

        # Install ceiling KUBEAGENTS_AUTOMERGE_MAX_RISK=low blocks medium in dev
        with mock.patch.dict(os.environ, {"KUBEAGENTS_AUTOMERGE_MAX_RISK": "low"}):
            with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
                r2_clamped = self._risk_label(broker, 2)
                self.assertFalse(r2_clamped["labelled"])
                self.assertEqual(self.forge.proposals[2]["labels"], ["risk:medium"])

        # Dial below Auto-merge blocks `on` label, while `practice` still applies `would-auto-merge`
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=False):
            r1_dial = self._risk_label(broker, 1)
            self.assertFalse(r1_dial["labelled"])
            self.assertEqual(self.forge.proposals[1]["labels"], ["risk:low"])
            # Revoking kube-agents:auto-merge also updates commit status to pending
            self.assertEqual(
                self.forge.statuses[(self.forge.proposals[1]["sourceRevision"], vcs_broker.RISK_AUTOMERGE_STATUS_CONTEXT)][0],
                "pending",
            )

            broker_prac = self._make_broker(switch="practice")
            r1_prac = self._risk_label(broker_prac, 1)
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
        self.assertEqual(res["disruptiveness"], "non_disruptive")

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
            res = self._risk_label(broker, 11)
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
            r_spoof = self._risk_label(broker, 20, cluster="my-gcp-project/us-east4/dev-c1")
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
            r_merge = self._risk_label(broker, 31)
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
            res = self._risk_label(broker, 45)
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
            res2 = self._risk_label(broker, 46)
        self.assertFalse(res2["labelled"])
        self.assertIn("invalid cluster directory name", res2["reason"])
        self.assertEqual(called, [])

    def test_arbitrary_risk_prefix_and_tuple_labels_cannot_be_forged(self) -> None:
        """Any `risk:*` label (including spoofed tiers like `risk:none`) and tuple payloads are refused."""
        for spoof in ("risk:none", "risk:safe", "RISK:ZERO", "  risk:minimal  "):
            for verb, payload in (
                ("proposal-create", {"title": "t", "labels": [spoof]}),
                ("proposal-update", {"number": 1, "labelsAdd": (spoof,)}),
                ("proposal-update", {"number": 1, "labelsRemove": [spoof]}),
                ("issue-create", {"title": "i", "labels": (f"bug,{spoof}",)}),
                ("issue-update", {"number": 2, "labelsAdd": [spoof]}),
                ("label-ensure", {"name": spoof}),
            ):
                refusal = automerge_eligibility.reserved_label_refusal(verb, payload)
                self.assertIsNotNone(refusal, f"{verb} {payload} must be refused")

    def test_expected_head_sha_pin_prevents_stale_api_toctou_and_strips_stale_labels(self) -> None:
        """If forge API still reports old SHA when caller pins new expected_head_sha, refuse and strip labels."""
        sha_low = self._push_branch(
            "platform-agent/toctou-sha",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        sha_crit = self._push_branch(
            "platform-agent/toctou-sha",
            {"clusters/dev-c1/web.yaml": None},  # Deleting Deployment -> destructive / high-critical
        )
        # Simulate eventual consistency: forge PR view still reports `sha_low` and stale labels!
        self.forge.proposals[50] = {
            "number": 50,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/toctou-sha",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha_low,
            "target": "main",
            "labels": ["risk:low", "kube-agents:auto-merge"],
        }
        broker = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            c_res = broker.proposal_classify_risk(
                {"repository": "acme/infra", "number": 50, "expected_head_sha": sha_crit}
            )
            self.assertEqual(c_res["risk_level"], "unknown")
            self.assertEqual(c_res["reasons"][0]["code"], "HEAD_SHA_MISMATCH")
            self.assertFalse(c_res["automerge"]["eligible_for_automerge"])

            l_res = broker.proposal_risk_label(
                {"repository": "acme/infra", "number": 50, "expected_head_sha": sha_crit}
            )
        self.assertEqual(l_res["risk_level"], "unknown")
        self.assertIsNone(l_res["risk_label"])
        self.assertFalse(l_res["labelled"])
        self.assertEqual(self.forge.proposals[50]["labels"], [])

    def test_git_branch_moved_ahead_of_forge_revision_fails_closed_without_expected_sha(self) -> None:
        """Even without expected_head_sha, if git branch tip != proposal.sourceRevision, fail closed!"""
        sha_low = self._push_branch(
            "platform-agent/lagging-api",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        # Advance git branch to delete workload while forge proposal metadata still says `sha_low`
        self._push_branch(
            "platform-agent/lagging-api",
            {"clusters/dev-c1/web.yaml": None},
        )
        self.forge.proposals[51] = {
            "number": 51,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/lagging-api",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha_low,
            "target": "main",
            "labels": ["risk:low", "kube-agents:auto-merge"],
        }
        broker = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            res = self._risk_label(broker, 51)
        self.assertEqual(res["risk_level"], "unknown")
        self.assertEqual(res["reasons"][0]["code"], "PROPOSAL_BRANCH_MOVED")
        self.assertFalse(res["labelled"])
        self.assertIsNone(res["risk_label"])
        self.assertEqual(self.forge.proposals[51]["labels"], [])

    def test_publish_and_proposal_create_return_4_axis_risk_summary_and_reconcile_push(self) -> None:
        """`proposal_create` and `publish` attach 4-axis `risk` summaries and revoke auto-merge on destructive push."""
        pvc_yaml = (
            "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n"
            "  name: web-data\n  namespace: shop\nspec:\n"
            "  accessModes: [ReadWriteOnce]\n  resources:\n    requests:\n      storage: 10Gi\n"
        )
        self._push_branch("main", {"clusters/dev-c1/data-pvc.yaml": pvc_yaml}, base="main")
        broker = self._make_broker(switch="on", auto_label_risk=True)

        # Build a valid git bundle for round 1 (low-risk replica bump 3 -> 4)
        clone_dir = self.root / "agent_clone"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(clone_dir)], check=True)
        subprocess.run(["git", "config", "user.email", "bot@example.com"], cwd=clone_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Bot"], cwd=clone_dir, check=True)
        base_rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone_dir, text=True).strip()
        subprocess.run(["git", "checkout", "-q", "-b", "platform-agent/e2e-push"], cwd=clone_dir, check=True)
        (clone_dir / "clusters" / "dev-c1" / "web.yaml").write_text(
            DEPLOY_V1.replace("replicas: 3", "replicas: 4"), encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=clone_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "bump 3->4"], cwd=clone_dir, check=True)
        b1 = self.root / "r1.bundle"
        subprocess.run(["git", "bundle", "create", str(b1), "platform-agent/e2e-push", f"^{base_rev}"], cwd=clone_dir, check=True)

        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            pub1 = broker.publish({
                "repository": "acme/infra",
                "branch": "platform-agent/e2e-push",
                "target": "main",
                "baseRevision": base_rev,
                "clonedFrom": "main",
                "bundleBase64": base64.b64encode(b1.read_bytes()).decode("ascii"),
            })
            created = broker.proposal_create({
                "repository": "acme/infra",
                "source": "platform-agent/e2e-push",
                "target": "main",
                "title": "Scale web",
            })

        num = created["proposal"]["number"]
        self.assertIn("risk", created)
        self.assertEqual(created["risk"]["risk_level"], "low")
        self.assertEqual(created["risk"]["risk_label"], "risk:low")
        self.assertEqual(created["risk"]["blast_radius"], "low")
        self.assertEqual(created["risk"]["revertability"], "easy")
        self.assertEqual(created["risk"]["disruptiveness"], "non_disruptive")
        self.assertTrue(created["risk"]["labelled"])
        self.assertEqual(sorted(created["proposal"]["labels"]), ["kube-agents:auto-merge", "risk:low"])

        # Round 2: push a destructive commit deleting a stateful PersistentVolumeClaim onto the same open PR!
        (clone_dir / "clusters" / "dev-c1" / "data-pvc.yaml").unlink()
        subprocess.run(["git", "add", "-A"], cwd=clone_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "delete stateful pvc"], cwd=clone_dir, check=True)
        tip2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone_dir, text=True).strip()
        b2 = self.root / "r2.bundle"
        subprocess.run(
            ["git", "bundle", "create", str(b2), "platform-agent/e2e-push", f"^{pub1['revision']}"],
            cwd=clone_dir,
            check=True,
        )
        # Update fake forge's sourceRevision on push when git push lands, or simulate forge updating at push
        orig_git = broker._bind({"repository": "acme/infra"}).git

        labels_at_push_moment: list[list[str]] = []

        def sync_forge_on_push(cwd: Path, *args: str, **kw: Any) -> subprocess.CompletedProcess:
            if args and args[0] == "push":
                labels_at_push_moment.append(list(self.forge.proposals[num]["labels"]))
            proc = orig_git(cwd, *args, **kw)
            if args and args[0] == "push" and proc.returncode == 0:
                self.forge.proposals[num]["sourceRevision"] = tip2
            return proc

        bound = broker._bind({"repository": "acme/infra"})
        bound_synced = vcs_broker.Binding(bound.forge, bound.repo, lambda: bound.api, sync_forge_on_push)
        broker._bind = lambda payload: bound_synced  # type: ignore[method-assign]

        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            pub2 = broker.publish({
                "repository": "acme/infra",
                "branch": "platform-agent/e2e-push",
                "target": "main",
                "baseRevision": pub1["revision"],
                "clonedFrom": "platform-agent/e2e-push",
                "advance": True,
                "bundleBase64": base64.b64encode(b2.read_bytes()).decode("ascii"),
            })

        self.assertIn("risk", pub2)
        self.assertEqual(labels_at_push_moment, [[]])
        self.assertEqual(pub2["risk"]["disruptiveness"], "destructive")
        self.assertFalse(pub2["risk"]["labelled"])
        self.assertNotIn("kube-agents:auto-merge", self.forge.proposals[num]["labels"])
        self.assertNotIn("risk:low", self.forge.proposals[num]["labels"])
        self.assertEqual(self.forge.proposals[num]["labels"], [pub2["risk"]["risk_label"]])

    def test_open_pr_failing_gate_strips_stale_labels_and_preserves_case_insensitive_match(self) -> None:
        """Stale labels are stripped when an open PR fails `_risk_proposal_gate`; matching case-folded label is preserved."""
        sha = self._push_branch(
            "platform-agent/retargeted",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        # Case 1: Open PR carrying mixed/multiple labels but targeting `release-1.0` != `main`
        self.forge.proposals[60] = {
            "number": 60,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/retargeted",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha,
            "target": "release-1.0",
            "labels": ["RISK:LOW", "risk:high", "kube-agents:auto-merge"],
        }
        broker = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            res = self._risk_label(broker, 60)
        self.assertFalse(res["labelled"])
        self.assertIsNone(res["risk_label"])
        self.assertEqual(self.forge.proposals[60]["labels"], [])
        self.assertEqual(res["applied_labels"], [])

        # Case 2: Open PR on `main` carrying `RISK:LOW` + stale `risk:high` + spoofed `risk:none`
        self.forge.proposals[61] = {
            "number": 61,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/retargeted",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha,
            "target": "main",
            "labels": ["RISK:LOW", "risk:high", "risk:none", "custom:keep"],
        }
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            res2 = self._risk_label(broker, 61)
        self.assertTrue(res2["labelled"])
        self.assertEqual(
            sorted(self.forge.proposals[61]["labels"]),
            ["RISK:LOW", "custom:keep", "kube-agents:auto-merge"],
        )

    def test_scratch_clone_ceiling_and_timeout_cleanup_leave_zero_temp_dirs(self) -> None:
        """Exceeding `max_clone_bytes` or hitting `TimeoutExpired` fails closed as `unknown` and cleans scratch."""
        sha = self._push_branch(
            "platform-agent/ceiling-pr",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        self.forge.proposals[70] = {
            "number": 70,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/ceiling-pr",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha,
            "target": "main",
            "labels": [],
        }
        broker = self._make_broker(switch="on")
        broker.max_clone_bytes = 1
        r_ceil = broker.proposal_classify_risk({"repository": "acme/infra", "number": 70})
        self.assertEqual(r_ceil["risk_level"], "unknown")
        self.assertEqual(r_ceil["reasons"][0]["code"], "CLONE_TOO_LARGE")
        self.assertEqual(list(self.scratch.iterdir()), [])

        # TimeoutExpired during git fetch -> fails closed and cleans up scratch dir!
        def timeout_git_runner(
            argv: list[str], cwd: Path, check: bool = True, config: tuple = (), **kw: Any
        ) -> subprocess.CompletedProcess:
            if "fetch" in argv:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
            return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=check)

        broker_to = vcs_broker.VcsBroker(
            self.scratch,
            git_runner=timeout_git_runner,
            cluster_read=self._cluster_read,
            automerge_switch=lambda: "on",
            base_branch="main",
        )
        bound = vcs_broker.Binding(
            self.forge,
            "acme/infra",
            lambda: mock.Mock(ensure=lambda: None, whoami=lambda: "kube-agents-bot"),
            broker_to._git_with(lambda: (), False),
        )
        broker_to._bind = lambda payload: bound  # type: ignore[method-assign]
        r_to = broker_to.proposal_classify_risk({"repository": "acme/infra", "number": 70})
        self.assertEqual(r_to["risk_level"], "unknown")
        self.assertEqual(r_to["reasons"][0]["code"], "GIT_CLASSIFICATION_ERROR")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_merged_pr_deleted_target_branch_and_ambiguous_or_octopus_parents(self) -> None:
        """Merged PRs handle deleted release branches via shallow mergeCommit fetch and fail closed on octopus/mismatched parents."""
        subprocess.run(
            ["git", "--git-dir", str(self.remote), "config", "uploadpack.allowReachableSHA1InWant", "true"],
            check=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(self.remote), "config", "uploadpack.allowAnySHA1InWant", "true"],
            check=True,
        )
        # 1. Merged PR onto `release-1.0`, then `release-1.0` AND feature branch are both deleted (while main still holds merge_sha or tag holds it)
        subprocess.run(["git", "checkout", "-q", "-B", "release-1.0", "main"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "release-1.0"], cwd=self.work, check=True)
        pr_sha = self._push_branch(
            "platform-agent/rel-fix",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
            base="release-1.0",
        )
        subprocess.run(["git", "checkout", "-q", "release-1.0"], cwd=self.work, check=True)
        subprocess.run(["git", "merge", "-q", "--no-ff", pr_sha, "-m", "Merge rel fix"], cwd=self.work, check=True)
        rel_merge_sha = self._rev("HEAD")
        # Tag it so remote GC keeps it reachable, then delete BOTH `release-1.0` and `platform-agent/rel-fix`!
        subprocess.run(["git", "tag", "archive/rel-1.0", rel_merge_sha], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "archive/rel-1.0"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "--delete", "release-1.0"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "--delete", "platform-agent/rel-fix"], cwd=self.work, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.work, check=True)

        self.forge.proposals[80] = {
            "number": 80,
            "state": "merged",
            "author": "kube-agents-bot",
            "source": "platform-agent/rel-fix",
            "sourceRepo": "acme/infra",
            "sourceRevision": pr_sha,
            "mergeCommit": rel_merge_sha,
            "target": "release-1.0",
            "labels": [],
        }
        # Use file:// remote URL so git honors shallow `--depth=2 <sha>` natively!
        self.forge._url = f"file://{self.remote}"
        broker = self._make_broker(switch="off")
        r_del_target = broker.proposal_classify_risk({"repository": "acme/infra", "number": 80})
        self.assertEqual(r_del_target["risk_level"], "low")
        self.assertEqual(r_del_target["disruptiveness"], "non_disruptive")

        # 2. Mismatched second parent (mergeCommit^2 != sourceRevision) fails closed!
        self.forge.proposals[81] = {
            **self.forge.proposals[80],
            "number": 81,
            "sourceRevision": "b" * 40,
        }
        r_mismatch_p2 = broker.proposal_classify_risk({"repository": "acme/infra", "number": 81})
        self.assertEqual(r_mismatch_p2["risk_level"], "unknown")
        self.assertEqual(r_mismatch_p2["reasons"][0]["code"], "MERGED_PR_AMBIGUOUS_PARENTS")

        # 3. Octopus merge (3 parents) fails closed!
        b_a = self._push_branch(
            "platform-agent/octo-a",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        b_b = self._push_branch(
            "platform-agent/octo-b",
            {"clusters/staging-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.work, check=True)
        subprocess.run(["git", "merge", "-q", "--no-ff", b_a, b_b, "-m", "Octopus merge"], cwd=self.work, check=True)
        octo_sha = self._rev("HEAD")
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.work, check=True)
        self.forge.proposals[82] = {
            "number": 82,
            "state": "merged",
            "author": "kube-agents-bot",
            "source": "platform-agent/octo-a",
            "sourceRepo": "acme/infra",
            "sourceRevision": b_a,
            "mergeCommit": octo_sha,
            "target": "main",
            "labels": [],
        }
        r_octo = broker.proposal_classify_risk({"repository": "acme/infra", "number": 82})
        self.assertEqual(r_octo["risk_level"], "unknown")
        self.assertEqual(r_octo["reasons"][0]["code"], "MERGED_PR_OCTOPUS_MERGE")

    def test_concurrent_classifications_are_thread_safe_and_leave_clean_scratch(self) -> None:
        """Concurrent `proposal_classify_risk` requests isolate scratch dirs and `out_context`."""
        sha_low = self._push_branch(
            "platform-agent/conc-low",
            {"clusters/test-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        sha_med = self._push_branch(
            "platform-agent/conc-med",
            {"clusters/dev-c1/worker.yaml": NEW_DEPLOY},
        )
        sha_prod = self._push_branch(
            "platform-agent/conc-prod",
            {"clusters/prod-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        self.forge.proposals[91] = {
            "number": 91, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/conc-low", "sourceRepo": "acme/infra",
            "sourceRevision": sha_low, "target": "main", "labels": [],
        }
        self.forge.proposals[92] = {
            "number": 92, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/conc-med", "sourceRepo": "acme/infra",
            "sourceRevision": sha_med, "target": "main", "labels": [],
        }
        self.forge.proposals[93] = {
            "number": 93, "state": "open", "author": "kube-agents-bot",
            "source": "platform-agent/conc-prod", "sourceRepo": "acme/infra",
            "sourceRevision": sha_prod, "target": "main", "labels": [],
        }
        broker = self._make_broker(switch="on")
        tasks = [91, 92, 93, 91, 92, 93, 91, 92]
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(
                    pool.map(
                        lambda n: broker.proposal_classify_risk({"repository": "acme/infra", "number": n}),
                        tasks,
                    )
                )
        expected = {
            91: ("low", True),
            92: ("medium", True),
            93: ("medium", False),
        }
        for n, res in zip(tasks, results):
            exp_risk, exp_elig = expected[n]
            self.assertEqual(res["risk_level"], exp_risk)
            self.assertEqual(res["automerge"]["eligible_for_automerge"], exp_elig)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_stale_base_diff_tree_truncation_or_nonzero_fails_closed(self) -> None:
        """If `diff-tree` during `stale_base` check is truncated or returns non-zero, `stale_base` fails closed."""
        pr_sha = self._push_branch(
            "platform-agent/stale-trunc",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        # Advance main on an UNRELATED file so `target_tip != diff_base`
        (self.work / "clusters" / "prod-c1" / "web.yaml").write_text(
            DEPLOY_V1.replace("replicas: 3", "replicas: 5"), encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=self.work, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance main unrelated"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.work, check=True)

        self.forge.proposals[95] = {
            "number": 95,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/stale-trunc",
            "sourceRepo": "acme/infra",
            "sourceRevision": pr_sha,
            "target": "main",
            "labels": [],
        }

        for mode in ("truncated", "nonzero"):
            def faulty_diff_tree_runner(
                argv: list[str], cwd: Path, check: bool = True, config: tuple = (), _mode: str = mode, **kw: Any
            ) -> subprocess.CompletedProcess:
                if "diff-tree" in argv and "--literal-pathspecs" in argv:
                    proc = subprocess.CompletedProcess(argv, 0 if _mode == "truncated" else 128, "", "err")
                    if _mode == "truncated":
                        proc.truncated = True  # type: ignore[attr-defined]
                    return proc
                return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=check)

            broker = vcs_broker.VcsBroker(
                self.scratch,
                git_runner=faulty_diff_tree_runner,
                cluster_read=self._cluster_read,
                automerge_switch=lambda: "on",
                base_branch="main",
            )
            bound = vcs_broker.Binding(
                self.forge,
                "acme/infra",
                lambda: mock.Mock(ensure=lambda: None, whoami=lambda: "kube-agents-bot"),
                broker._git_with(lambda: (), False),
            )
            broker._bind = lambda payload: bound  # type: ignore[method-assign]
            with self.subTest(mode=mode), mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
                res = self._risk_label(broker, 95)
                self.assertTrue(res["stale_base"])
                self.assertFalse(res["labelled"])
                self.assertIn("rebase", res["reason"])

    def test_excluded_cluster_and_same_name_steering_refused_and_switch_off_skips_rules(self) -> None:
        """Excluded clusters, caller project steering, and switch=off rules reads are blocked."""
        import scope_exclude

        sha = self._push_branch(
            "platform-agent/dev-excl",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        self.forge.proposals[96] = {
            "number": 96,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/dev-excl",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha,
            "target": "main",
            "labels": [],
        }

        # 1. With KUBEAGENTS_AUTOMERGE=off, `.kube-agents/automation.yaml` is NOT read,
        #    and a base-lookup error in out_context preserves the real `risk:low` verdict.
        broker_off = self._make_broker(switch="off")
        with mock.patch.object(
            broker_off, "_read_rules_from_bare", side_effect=AssertionError("automation.yaml read while off")
        ):
            c_off = broker_off.proposal_classify_risk({"repository": "acme/infra", "number": 96})
            self.assertEqual(c_off["risk_level"], "low")
            l_off = self._risk_label(broker_off, 96)
            self.assertEqual(l_off["risk_level"], "low")
            self.assertEqual(self.forge.proposals[96]["labels"], ["risk:low"])

        with mock.patch.object(broker_off, "_default_branch_of_remote", side_effect=OSError("symref failed")):
            broker_off.base_branch = ""
            l_base_err = self._risk_label(broker_off, 96)
            self.assertEqual(l_base_err["risk_level"], "low")
            self.assertFalse(l_base_err["labelled"])

        # 2. Excluded cluster in `spec.scope.exclude.clusters` is refused auto-merge labelling
        scope_path = self.root / "scope.json"
        scope_path.write_text(
            json.dumps(
                {
                    "exclude": {
                        "clusters": [
                            {"projectId": "acme-proj", "location": "us-east4", "clusterName": "dev-c1"}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        excl = scope_exclude.Exclusions(path=lambda: str(scope_path))
        self.clusters_list = [
            {
                "name": "dev-c1",
                "location": "us-east4",
                "selfLink": "https://container.googleapis.com/v1/projects/acme-proj/locations/us-east4/clusters/dev-c1",
                "resourceLabels": {"environment": "dev"},
            }
        ]
        broker_excl = vcs_broker.VcsBroker(
            self.scratch,
            git_runner=_local_git_runner,
            cluster_read=self._cluster_read,
            cluster_exclusions=excl,
            automerge_switch=lambda: "on",
            base_branch="main",
        )
        bound = vcs_broker.Binding(
            self.forge,
            "acme/infra",
            lambda: mock.Mock(ensure=lambda: None, whoami=lambda: "kube-agents-bot"),
            broker_excl._git_with(lambda: (), False),
        )
        broker_excl._bind = lambda payload: bound  # type: ignore[method-assign]
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            r_excl = self._risk_label(broker_excl, 96)
            self.assertFalse(r_excl["labelled"])
            self.assertEqual(r_excl["reason"], "the cluster's labels could not be read")

        # 3. Ambiguous same-named clusters cannot be steered by caller-supplied `cluster`
        self.clusters_list = [
            {
                "name": "dev-c1",
                "location": "us-east4",
                "selfLink": "https://container.googleapis.com/v1/projects/dev-proj/locations/us-east4/clusters/dev-c1",
                "resourceLabels": {"environment": "dev"},
            },
            {
                "name": "dev-c1",
                "location": "us-east4",
                "selfLink": "https://container.googleapis.com/v1/projects/prod-proj/locations/us-east4/clusters/dev-c1",
                "resourceLabels": {"environment": "prod"},
            },
        ]
        broker_ambig = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            r_steer = self._risk_label(broker_ambig, 96, cluster="dev-proj/us-east4/dev-c1")
            self.assertFalse(r_steer["labelled"])
            self.assertEqual(r_steer["reason"], "the cluster's labels could not be read")

            # Non-string `cluster` coordinate fails closed as invalid
            r_bad_type = self._risk_label(broker_ambig, 96, cluster=123)
            self.assertFalse(r_bad_type["labelled"])
            self.assertEqual(r_bad_type["reason"], "invalid cluster coordinate")

            # Unverifiable project (missing selfLink + unset GKE_PROJECT_ID) refuses explicit `cluster`
            self.clusters_list = [
                {"name": "dev-c1", "location": "us-east4", "resourceLabels": {"environment": "dev"}}
            ]
            r_unverified = self._risk_label(broker_ambig, 96, cluster="evil-proj/us-east4/dev-c1")
            self.assertFalse(r_unverified["labelled"])
            self.assertEqual(r_unverified["reason"], "the cluster's labels could not be read")

    def test_unset_ceiling_disables_risk_tier_and_status_failure_or_exception_fails_closed_and_deepens(self) -> None:
        """Unset KUBEAGENTS_AUTOMERGE_MAX_RISK stays off, status errors hold back auto-merge, exceptions strip stale labels, and >64-commit branches deepen."""
        from workspace_paths import WorkspaceError

        sha = self._push_branch(
            "platform-agent/dev-status",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        self.forge.proposals[97] = {
            "number": 97,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/dev-status",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha,
            "target": "main",
            "labels": [],
        }
        broker = self._make_broker(switch="on")

        # 1. Missing expected_head_sha on write verb is refused
        with self.assertRaises(WorkspaceError):
            broker.proposal_risk_label({"repository": "acme/infra", "number": 97})

        # 2. Unset KUBEAGENTS_AUTOMERGE_MAX_RISK disables risk-tier auto-merge even when KUBEAGENTS_AUTOMERGE=on
        env_no_ceiling = {k: v for k, v in os.environ.items() if k != "KUBEAGENTS_AUTOMERGE_MAX_RISK"}
        with (
            mock.patch.dict(os.environ, env_no_ceiling, clear=True),
            mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True),
        ):
            r_unset = self._risk_label(broker, 97)
            self.assertEqual(r_unset["risk_level"], "low")
            self.assertFalse(r_unset["labelled"])
            self.assertEqual(self.forge.proposals[97]["labels"], ["risk:low"])

        # 3. Failing to post kube-agents/auto-merge commit status holds back kube-agents:auto-merge
        self.forge.fail_status_contexts.add(vcs_broker.RISK_AUTOMERGE_STATUS_CONTEXT)
        try:
            with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
                r_st_fail = self._risk_label(broker, 97)
                self.assertFalse(r_st_fail["labelled"])
                self.assertNotIn("kube-agents:auto-merge", self.forge.proposals[97]["labels"])
        finally:
            self.forge.fail_status_contexts.clear()

        # 4. Unexpected KeyError inside classifier fails closed to risk:unknown and strips stale auto-merge
        self.forge.proposals[97]["labels"] = ["risk:low", "kube-agents:auto-merge"]
        with (
            mock.patch.object(vcs_broker.risk_classifier, "classify_git_range", side_effect=KeyError("boom")),
            mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True),
        ):
            r_exc = self._risk_label(broker, 97)
            self.assertEqual(r_exc["risk_level"], "unknown")
            self.assertFalse(r_exc["labelled"])
            self.assertEqual(self.forge.proposals[97]["labels"], ["risk:unknown"])

        # 5. Deeply diverged branch (>64 commits on main) deepens shallow fetch cleanly
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.work, check=True)
        for idx in range(70):
            (self.work / "README.md").write_text(f"tick {idx}\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=self.work, check=True)
            subprocess.run(["git", "commit", "-q", "-m", f"tick {idx}"], cwd=self.work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.work, check=True)
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            r_deep = self._risk_label(broker, 97)
            self.assertEqual(r_deep["risk_level"], "low")
            self.assertTrue(r_deep["labelled"])
            self.assertEqual(sorted(self.forge.proposals[97]["labels"]), ["kube-agents:auto-merge", "risk:low"])

    def test_commit_statuses_reach_text_label_and_labelless_forges_without_touching_labels(self) -> None:
        """Forges where text_sets_labels=True or labels=False still receive SHA-bound commit statuses on create and publish."""
        sha = self._push_branch(
            "platform-agent/status-only",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        for mode_name, has_labels, text_sets in (("text-labels", True, True), ("no-labels", False, False)):
            with self.subTest(mode=mode_name):
                self.forge.labels = has_labels
                self.forge.text_sets_labels = text_sets
                self.forge.statuses.clear()
                self.forge.label_adds.clear()
                self.forge.ensured_labels.clear()
                self.forge.proposals[110] = {
                    "number": 110,
                    "state": "open",
                    "author": "kube-agents-bot",
                    "source": "platform-agent/status-only",
                    "sourceRepo": "acme/infra",
                    "sourceRevision": sha,
                    "target": "main",
                    "labels": [],
                }
                broker = self._make_broker(switch="on", auto_label_risk=True)
                with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
                    res = self._risk_label(broker, 110)
                    cls_res = broker.proposal_classify_risk({"repository": "acme/infra", "number": 110})
                self.assertEqual(res["risk_level"], "low")
                self.assertTrue(res["commit_status_posted"])
                self.assertTrue(res["automerge_status_posted"])
                self.assertFalse(res["labelled"])
                self.assertIsNone(res["risk_label"])
                self.assertEqual(self.forge.label_adds, [])
                self.assertEqual(self.forge.ensured_labels, [])
                self.assertIn((sha, vcs_broker.RISK_VERDICT_STATUS_CONTEXT), self.forge.statuses)
                self.assertEqual(
                    self.forge.statuses[(sha, vcs_broker.RISK_AUTOMERGE_STATUS_CONTEXT)][0],
                    "success",
                )
                self.assertTrue(cls_res["automerge"]["eligible_for_automerge"])
                self.assertIsNone(cls_res["automerge"]["would_label"])
                # `_drop_practice_label` still collects open proposal numbers for status re-evaluation on `publish`
                bound = broker._bind({"repository": "acme/infra"})
                self.assertEqual(broker._drop_practice_label(bound, "platform-agent/status-only"), [110])
        self.forge.labels = True
        self.forge.text_sets_labels = False

    def test_pod_restarting_and_runtime_medium_edits_withheld_from_automerge(self) -> None:
        """Pod-restarting image/resource edits and runtime-risky medium edits never receive auto-merge."""
        cm_base = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-cm\n  namespace: shop\ndata:\n  MODE: fast\n"
        self._push_branch("main", {"clusters/dev-c1/cm.yaml": cm_base}, base="main")
        cases = (
            ("img-bump", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("v1.0.0", "v1.0.1")}, "low", "disruptive", "easy"),
            ("res-bump", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace('cpu: "100m"', 'cpu: "150m"')}, "low", "disruptive", "easy"),
            ("res-reduce", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace('cpu: "100m"', 'cpu: "80m"')}, "medium", "disruptive", "moderate"),
            ("scale-down", {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 2")}, "medium", "non_disruptive", "moderate"),
            ("cm-mod", {"clusters/dev-c1/cm.yaml": cm_base.replace("MODE: fast", "MODE: slow")}, "medium", "non_disruptive", "moderate"),
        )
        broker = self._make_broker(switch="on")
        for idx, (slug, edits, exp_level, exp_disrupt, exp_revert) in enumerate(cases, start=120):
            with self.subTest(case=slug):
                sha = self._push_branch(f"platform-agent/{slug}", edits)
                self.forge.proposals[idx] = {
                    "number": idx,
                    "state": "open",
                    "author": "kube-agents-bot",
                    "source": f"platform-agent/{slug}",
                    "sourceRepo": "acme/infra",
                    "sourceRevision": sha,
                    "target": "main",
                    "labels": [],
                }
                with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
                    res = self._risk_label(broker, idx)
                self.assertEqual(res["risk_level"], exp_level)
                self.assertEqual(res["disruptiveness"], exp_disrupt)
                self.assertEqual(res["revertability"], exp_revert)
                self.assertFalse(res["labelled"], f"{slug} must not auto-merge")
                self.assertNotIn("kube-agents:auto-merge", self.forge.proposals[idx]["labels"])

    def test_edit_manifest_recipe_pr_never_falls_through_to_max_risk(self) -> None:
        """A verified `edit-manifest` recipe PR is blocked from falling through to `max_risk` on both broker paths."""
        import types
        from fix_recipes import registry as fix_reg

        fake_recipe = fix_reg.Recipe(
            kind="scale-floor",
            name="scale floor",
            title="Scale floor",
            dial_environment="dev",
            verify="edit-manifest",
            edit_api_version="apps/v1",
            edit_kind="Deployment",
            edit_paths="spec.replicas",
        )
        fake_fix_mod = types.SimpleNamespace(allowed_edit=lambda path, old, new: True)

        sha = self._push_branch(
            "platform-agent/edit-recipe",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        self.forge.proposals[130] = {
            "number": 130,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/edit-recipe",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha,
            "target": "main",
            "labels": [],
        }
        broker = self._make_broker(switch="on")
        with (
            mock.patch.object(fix_reg, "recipes", return_value=(*fix_reg.recipes(), fake_recipe)),
            mock.patch.object(fix_reg, "generator", return_value=fake_fix_mod),
            mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True),
        ):
            prac = broker.proposal_practice_label({"repository": "acme/infra", "number": 130})
            self.assertFalse(prac["labelled"])
            self.assertIn("scale-floor recipe's", prac["reason"])

            risk_res = self._risk_label(broker, 130)
            self.assertEqual(risk_res["risk_level"], "low")
            self.assertFalse(risk_res["labelled"])
            self.assertIn("fix recipe", risk_res["reason"])
            self.assertEqual(self.forge.proposals[130]["labels"], ["risk:low"])

    def test_declared_environments_paths_and_multi_project_gke_lookup(self) -> None:
        """Repos using `envs/dev/**` or `apps/*/envs/dev/**` and multi-project `GKE_PROJECTS` fleets resolve cleanly."""
        flex_rules = """\
version: 1
environments:
  dev:
    paths: ["envs/dev/**", "apps/*/envs/dev/**"]
    branches: [main]
  prod:
    paths: ["envs/prod/**"]
automerge:
  allow:
    - max_risk: low
      environments: [dev]
  deny:
    - fix: "*"
      environments: [prod]
"""
        self._push_branch(
            "main",
            {
                ".kube-agents/automation.yaml": flex_rules,
                "envs/dev/web.yaml": DEPLOY_V1,
                "apps/shop/envs/dev/web.yaml": DEPLOY_V1,
                "envs/prod/web.yaml": DEPLOY_V1,
            },
            base="main",
        )
        sha_overlay = self._push_branch(
            "platform-agent/overlay-bump",
            {
                "envs/dev/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4"),
                "apps/shop/envs/dev/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4"),
            },
        )
        self.forge.proposals[140] = {
            "number": 140,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/overlay-bump",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha_overlay,
            "target": "main",
            "labels": [],
        }
        broker = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            r_ov = self._risk_label(broker, 140)
        self.assertEqual(r_ov["risk_level"], "low")
        self.assertTrue(r_ov["labelled"])
        self.assertEqual(sorted(self.forge.proposals[140]["labels"]), ["kube-agents:auto-merge", "risk:low"])

        # Multi-project `GKE_PROJECTS` resolves `dev-c1` across projects cleanly
        sha_mp = self._push_branch(
            "platform-agent/mp-bump",
            {"clusters/dev-c1/web.yaml": DEPLOY_V1.replace("replicas: 3", "replicas: 4")},
        )
        self.forge.proposals[141] = {
            "number": 141,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/mp-bump",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha_mp,
            "target": "main",
            "labels": [],
        }

        def mp_cluster_read(argv: list[str], context: str) -> tuple[int, str, str]:
            proj = argv[argv.index("--project") + 1] if "--project" in argv else ""
            if proj == "empty-fleet-proj":
                return 0, "[]", ""
            if proj == "my-gcp-project":
                return self._cluster_read(argv, context)
            return 1, "", "unexpected project"

        broker._cluster_read = mp_cluster_read
        with (
            mock.patch.dict(os.environ, {"GKE_PROJECTS": "empty-fleet-proj,my-gcp-project"}),
            mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True),
        ):
            r_mp = self._risk_label(broker, 141)
        self.assertTrue(r_mp["labelled"])

    def test_packaging_unsupported_posts_unsupported_status_without_noisy_risk_unknown_label(self) -> None:
        """Helm/Kustomize/Terraform PRs record `unsupported` status without adding `risk:unknown`, while parse errors still apply `risk:unknown`."""
        sha_pkg = self._push_branch(
            "platform-agent/helm-vals",
            {"charts/web/values.yaml": "replicaCount: 4\n"},
        )
        self.forge.proposals[150] = {
            "number": 150,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/helm-vals",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha_pkg,
            "target": "main",
            "labels": ["risk:low", "kube-agents:auto-merge"],
        }
        broker = self._make_broker(switch="on")
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            r_pkg = self._risk_label(broker, 150)
        self.assertEqual(r_pkg["risk_level"], "unknown")
        self.assertFalse(r_pkg["supported"])
        self.assertIsNone(r_pkg["risk_label"])
        self.assertFalse(r_pkg["labelled"])
        self.assertEqual(self.forge.proposals[150]["labels"], [])
        st_state, st_desc = self.forge.statuses[(sha_pkg, vcs_broker.RISK_VERDICT_STATUS_CONTEXT)]
        self.assertEqual(st_state, "success")
        self.assertTrue(st_desc.startswith("unsupported "), st_desc)

        # Genuine YAML syntax error still applies `risk:unknown` PR label!
        sha_bad_yaml = self._push_branch(
            "platform-agent/bad-yaml",
            {"clusters/dev-c1/broken.yaml": "apiVersion: v1\nkind: [unclosed\n"},
        )
        self.forge.proposals[151] = {
            "number": 151,
            "state": "open",
            "author": "kube-agents-bot",
            "source": "platform-agent/bad-yaml",
            "sourceRepo": "acme/infra",
            "sourceRevision": sha_bad_yaml,
            "target": "main",
            "labels": [],
        }
        with mock.patch.object(vcs_broker, "dial_allows_automerge", return_value=True):
            r_bad = self._risk_label(broker, 151)
        self.assertEqual(r_bad["risk_level"], "unknown")
        self.assertEqual(r_bad["risk_label"], "risk:unknown")
        self.assertEqual(self.forge.proposals[151]["labels"], ["risk:unknown"])


class CommitStatusForgeTests(unittest.TestCase):
    """Verify broker-internal `commit_status_set` across forge providers via the public `providers` surface."""

    def test_commit_status_set_on_supported_and_unsupported_forges(self) -> None:
        from providers import AVAILABLE, Forge, ForgeUnsupported, StubForge
        from workspace_paths import WorkspaceError

        testdata = Path(__file__).resolve().parent / "testdata" / "providers"
        by_name: dict[str, Any] = {}
        for cls in AVAILABLE:
            cfg_file = testdata / cls.name / "config.json"
            cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
            insts = list(cls.for_config(cfg))
            self.assertTrue(insts)
            by_name[cls.name] = insts[0]

        self.assertTrue(
            {"github", "gitlab", "gitea", "bitbucket", "bitbucket-datacenter"}.issubset(set(by_name.keys()))
        )
        sha = "a" * 40
        for name in ("github", "gitlab", "gitea", "bitbucket", "bitbucket-datacenter"):
            inst = by_name[name]
            self.assertTrue(inst.commit_statuses)
            calls: list[tuple] = []

            def fake_api(method: str, path: str, *, body: Any = None, **kw: Any) -> dict[str, Any]:
                calls.append((method, path, body))
                return {}

            ans = inst.commit_status_set(
                fake_api,
                "acme/infra",
                {
                    "revision": sha,
                    "state": "failure",
                    "context": "kube-agents/auto-merge",
                    "description": "x" * 200,
                },
            )
            self.assertEqual(ans["status"]["revision"], sha)
            self.assertEqual(len(ans["status"]["description"]), 140)
            self.assertEqual(len(calls), 1)
            method, path, body = calls[0]
            self.assertEqual(method, "POST")
            self.assertIn(sha, path)
            if name == "gitlab":
                self.assertEqual(body["state"], "failed")
                self.assertEqual(body["name"], "kube-agents/auto-merge")
            elif name in ("bitbucket", "bitbucket-datacenter"):
                self.assertEqual(body["state"], "FAILED")
                self.assertEqual(body["key"], "kube-agents/auto-merge")
                self.assertEqual(body["name"], "kube-agents/auto-merge")
            else:
                self.assertEqual(body["state"], "failure")
                self.assertEqual(body["context"], "kube-agents/auto-merge")

            for bad_payload in (
                {"revision": "short", "state": "success", "context": "kube-agents/risk"},
                {"revision": sha, "state": "bogus", "context": "kube-agents/risk"},
                {"revision": sha, "state": "success", "context": "bad context!"},
            ):
                with self.assertRaises(WorkspaceError):
                    inst.commit_status_set(fake_api, "acme/infra", bad_payload)

        with self.assertRaises(ForgeUnsupported):
            Forge().commit_status_set(lambda *a, **k: {}, "acme/infra", {"revision": sha, "state": "success", "context": "c"})
        stub = StubForge("stub", ("stub.example",), "pull request", ("missing token",))
        with self.assertRaises(ForgeUnsupported):
            stub.commit_status_set(lambda *a, **k: {}, "acme/infra", {"revision": sha, "state": "success", "context": "c"})


if __name__ == "__main__":
    unittest.main()
