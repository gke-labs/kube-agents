"""Unit tests for decision_memory.py. No model calls, no network: git runs on a
temporary repository and Hindsight is a local fake HTTP server.

A live run against a real Hindsight is opt-in: set DECISION_MEMORY_LIVE_URL.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(__file__))
import decision_memory as dm  # noqa: E402

DEPLOY = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: payments
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: app
          image: checkout:1.4.2
          resources:
            requests:
              memory: {mem}
"""

QUOTA = """apiVersion: v1
kind: ResourceQuota
metadata:
  name: payments
  namespace: payments
spec:
  hard:
    requests.memory: {mem}
"""

LESSON = """---
type: lesson
id: LES-2026-001
title: checkout OOM-kills below 256Mi under the JVM's off-heap growth
status: active
date: 2026-09-10
scope:
  paths: ["clusters/*/payments/checkout.yaml"]
  resources: ["Deployment/payments/checkout"]
  clusters: [prod-eu]
  checks: [oom-kill]
guard:
  rule: Keep checkout's memory request at or above 256Mi.
  severity: block
  expect:
    file: "clusters/*/payments/checkout.yaml"
    path: "spec.template.spec.containers[name=app].resources.requests.memory"
    op: ">="
    value: 256Mi
evidence:
  pull_request: https://github.com/acme/gitops/pull/31
  commit: {commit}
  merged_by: alice
---

## What happened

checkout was cut to 64Mi in a cost pass and crash-looped.
"""

DECISION = """---
type: decision
id: DEC-2026-001
title: The payments quota is a capacity cap, not a knob
status: active
date: 2026-08-01
scope:
  paths: ["clusters/*/payments/quota.yaml"]
  resources: ["ResourceQuota/payments/*"]
guard:
  rule: Do not raise the payments memory quota above 832Mi without a capacity review; fix the over-request instead.
  severity: warn
  expect:
    file: "clusters/*/payments/quota.yaml"
    path: 'spec.hard.requests\\.memory'
    op: "<="
    value: 832Mi
evidence:
  pull_request: https://github.com/acme/gitops/pull/7
---

## Context

The node pool is sized for 832Mi of payments requests.
"""


def sh(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class Repo:
    """A throwaway GitOps repository with a lesson learned from a merged fix."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        sh(self.path, "init", "-q", "-b", "main")
        sh(self.path, "config", "user.email", "t@example.com")
        sh(self.path, "config", "user.name", "t")
        self.write("clusters/prod-eu/payments/checkout.yaml", DEPLOY.format(mem="64Mi"))
        self.write("clusters/prod-eu/payments/quota.yaml", QUOTA.format(mem="832Mi"))
        self.write("knowledge/decisions/DEC-2026-001-payments-quota.md", DECISION)
        self.commit("payments: initial (#5)")
        self.write("clusters/prod-eu/payments/checkout.yaml", DEPLOY.format(mem="256Mi"))
        self.fix = self.commit("fix(checkout): restore 256Mi memory request (#31)")
        self.write("knowledge/lessons/LES-2026-001-checkout-oom.md", LESSON.format(commit=self.fix))
        self.commit("knowledge: LES-2026-001 (#31)")

    def write(self, rel, text):
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg):
        sh(self.path, "add", "-A")
        sh(self.path, "commit", "-q", "-m", msg)
        return sh(self.path, "rev-parse", "HEAD").strip()

    def run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = dm.main(["--repo", str(self.path), *argv])
        out = buf.getvalue()
        try:
            return rc, json.loads(out)
        except json.JSONDecodeError:
            return rc, out

    def close(self):
        self.tmp.cleanup()


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.close()

    def test_validate_accepts_fixture(self):
        rc, out = self.r.run("validate")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["records"], 2)

    def test_validate_rejects_unscoped_and_unevidenced_lesson(self):
        self.r.write("knowledge/lessons/LES-2026-002-x.md",
                     "---\ntype: lesson\nid: LES-2026-002\ntitle: x\nstatus: active\n---\nbody\n")
        rc, out = self.r.run("validate")
        self.assertEqual(rc, 1)
        errs = out["problems"]["knowledge/lessons/LES-2026-002-x.md"]
        self.assertTrue(any("scope" in e for e in errs))
        self.assertTrue(any("pull_request" in e for e in errs))

    def test_tags_carry_scope_and_status(self):
        rec = [r for r in dm.load_records(self.r.path) if r.id == "LES-2026-001"][0]
        tags = rec.tags()
        for t in ("scope:shared", "kind:lesson", "status:active", "id:LES-2026-001",
                  "cluster:prod-eu", "check:oom-kill", "res:Deployment/payments/checkout", "guard:block"):
            self.assertIn(t, tags)

    def test_index_text_keeps_identifiers(self):
        rec = [r for r in dm.load_records(self.r.path) if r.id == "LES-2026-001"][0]
        text = rec.render_for_index()
        self.assertIn("LES-2026-001", text)
        self.assertIn("https://github.com/acme/gitops/pull/31", text)
        self.assertIn("256Mi", text)


class GuardTests(unittest.TestCase):
    def test_quantities(self):
        self.assertTrue(dm.compare("512Mi", ">=", "256Mi"))
        self.assertFalse(dm.compare("64Mi", ">=", "256Mi"))
        self.assertTrue(dm.compare("1Gi", ">", "1000Mi"))
        self.assertTrue(dm.compare("500m", "<", "1"))
        self.assertFalse(dm.compare("lots", ">=", "1"))

    def test_selector_path(self):
        doc = {"spec": {"containers": [{"name": "side", "x": 1}, {"name": "app", "x": 2}]}}
        self.assertEqual(dm.yaml_get(doc, "spec.containers[name=app].x"), 2)
        self.assertIsNone(dm.yaml_get(doc, "spec.containers[name=nope].x"))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        sh(self.r.path, "checkout", "-q", "-b", "change")

    def tearDown(self):
        self.r.close()

    def test_reverting_a_learned_fix_blocks(self):
        self.r.write("clusters/prod-eu/payments/checkout.yaml", DEPLOY.format(mem="64Mi"))
        self.r.commit("chore: trim checkout memory")
        rc, out = self.r.run("check", "--base", "main")
        self.assertEqual(rc, 2)
        self.assertEqual(out["verdict"], "block")
        f = out["findings"][0]
        self.assertEqual((f["id"], f["verdict"]), ("LES-2026-001", "violates"))
        self.assertEqual(f["guard_results"][0]["actual"], "64Mi")
        self.assertIn("LES-2026-001", out["markdown"])
        self.assertIn("pull/31", out["markdown"])

    def test_raising_quota_warns_against_decision(self):
        self.r.write("clusters/prod-eu/payments/quota.yaml", QUOTA.format(mem="1200Mi"))
        self.r.commit("fix: raise payments quota")
        rc, out = self.r.run("check", "--base", "main")
        self.assertEqual(rc, 0)
        self.assertEqual(out["verdict"], "warn")
        self.assertEqual(out["findings"][0]["id"], "DEC-2026-001")

    def test_consistent_change_is_informational(self):
        self.r.write("clusters/prod-eu/payments/checkout.yaml", DEPLOY.format(mem="512Mi"))
        self.r.commit("feat: more memory")
        rc, out = self.r.run("check", "--base", "main")
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["findings"][0]["verdict"], "consistent")

    def test_resource_match_catches_a_moved_file(self):
        self.r.write("clusters/prod-us/apps/checkout-us.yaml", DEPLOY.format(mem="64Mi"))
        self.r.commit("feat: checkout in us")
        rc, out = self.r.run("check", "--base", "main")
        self.assertEqual([f["id"] for f in out["findings"]], ["LES-2026-001"])
        # Path scope does not match the new file, so the guard is not evaluated there:
        self.assertEqual(out["findings"][0]["verdict"], "related")

    def test_unrelated_change(self):
        self.r.write("clusters/prod-eu/web/frontend.yaml", "kind: ConfigMap\nmetadata: {name: f}\n")
        self.r.commit("feat: frontend")
        rc, out = self.r.run("check", "--base", "main")
        self.assertEqual((rc, out["verdict"], out["findings"]), (0, "ok", []))

    def test_deleting_guarded_file_violates(self):
        os.remove(self.r.path / "clusters/prod-eu/payments/checkout.yaml")
        self.r.commit("chore: drop checkout")
        rc, out = self.r.run("check", "--base", "main")
        self.assertEqual(out["findings"][0]["verdict"], "violates")

    def test_superseded_records_do_not_match(self):
        p = self.r.path / "knowledge/decisions/DEC-2026-001-payments-quota.md"
        p.write_text(p.read_text().replace("status: active", "status: superseded\nsuperseded_by: DEC-2026-001"))
        self.r.write("clusters/prod-eu/payments/quota.yaml", QUOTA.format(mem="1200Mi"))
        self.r.commit("x")
        rc, out = self.r.run("check", "--base", "main")
        self.assertEqual(out["findings"], [])


class WhyAndOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.close()

    def test_why_returns_record_and_history(self):
        rc, out = self.r.run("why", "clusters/prod-eu/payments/checkout.yaml")
        self.assertEqual([x["id"] for x in out["records"]], ["LES-2026-001"])
        self.assertEqual(out["history"][0]["pull_request"], "31")
        self.assertEqual([h["pull_request"] for h in out["history_without_record"]], ["5"])

    def test_why_accepts_multiple_paths_and_flag_path(self):
        rc, out = self.r.run(
            "why",
            "clusters/prod-eu/payments/checkout.yaml",
            "--path", "clusters/prod-eu/payments/quota.yaml",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "found")
        self.assertEqual(sorted(x["id"] for x in out["records"]), ["DEC-2026-001", "LES-2026-001"])
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(len(out["citations"]), 2)

    def test_why_works_on_content_directory_without_git(self):
        import shutil
        shutil.rmtree(self.r.path / ".git")
        rc, out = self.r.run("why", "clusters/prod-eu/payments/checkout.yaml")
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "found")
        self.assertEqual([x["id"] for x in out["records"]], ["LES-2026-001"])
        self.assertEqual(out["history"], [])
        self.assertTrue(any("LES-2026-001" in c for c in out["citations"]))

    def test_why_resolves_repo_slug_via_vcs_root(self):
        import shutil
        vcs_root = self.r.path.parent / "vcs_root"
        checkout = vcs_root / "github__acme__gitops__main"
        shutil.copytree(self.r.path, checkout)
        old_env = os.environ.get("KUBE_AGENTS_VCS_ROOT")
        os.environ["KUBE_AGENTS_VCS_ROOT"] = str(vcs_root)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = dm.main(["--repo", "acme/gitops", "why", "clusters/prod-eu/payments/checkout.yaml"])
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual(out["status"], "found")
            self.assertEqual([x["id"] for x in out["records"]], ["LES-2026-001"])
        finally:
            if old_env is None:
                os.environ.pop("KUBE_AGENTS_VCS_ROOT", None)
            else:
                os.environ["KUBE_AGENTS_VCS_ROOT"] = old_env
            shutil.rmtree(vcs_root, ignore_errors=True)

    def test_bootstrap_records_rejected_proposal_on_cited_records(self):
        prs = [
            {
                "number": 270,
                "title": "perf(payments): lower checkout memory to 64Mi",
                "state": "closed",
                "url": "https://github.com/acme/gitops/pull/270",
                "body": "Lower checkout memory request.",
                "_comments": [
                    {
                        "user": "reviewer",
                        "body": "Rejected: lowering memory violates LES-2026-001.",
                    }
                ],
            }
        ]
        prs_file = self.r.path / "rejected_prs.json"
        issues_file = self.r.path / "rejected_issues.json"
        prs_file.write_text(json.dumps(prs))
        issues_file.write_text("[]")
        rc, out = self.r.run(
            "bootstrap",
            "--prs-json", str(prs_file),
            "--issues-json", str(issues_file),
            "--write",
        )
        self.assertEqual(rc, 0)
        rec = dm.parse_record((self.r.path / "knowledge/lessons/LES-2026-001-checkout-oom.md").read_text(), "x")
        self.assertEqual(
            rec.evidence.get("rejected_proposals"),
            ["https://github.com/acme/gitops/pull/270 (#270)"],
        )
        rc_why, out_why = self.r.run("why", "clusters/prod-eu/payments/checkout.yaml")
        self.assertEqual(
            out_why["records"][0]["rejected_proposals"],
            ["https://github.com/acme/gitops/pull/270 (#270)"],
        )

    def test_why_without_record_says_so(self):
        self.r.write("clusters/prod-eu/web/frontend.yaml", "kind: ConfigMap\nmetadata: {name: f}\n")
        self.r.commit("feat: frontend (#40)")
        rc, out = self.r.run("why", "clusters/prod-eu/web/frontend.yaml")
        self.assertEqual(out["records"], [])
        self.assertIn("not of why", out["note"])

    def test_from_outcome_drafts_a_valid_lesson(self):
        outcome = {
            "pull_request": "https://github.com/acme/gitops/pull/44",
            "title": "payments quota exhaustion blocked checkout rollout",
            "merged_at": "2026-10-12T10:00:00Z", "merged_by": "bob", "commit": "abc123",
            "files": ["clusters/prod-eu/payments/checkout.yaml"], "cluster": "prod-eu",
            "check": "quota-exceeded", "finding_id": "quota-exceeded.prod-eu.payments.checkout",
            "root_cause": "Request inflated to 256Mi x4 exceeded the 832Mi quota.",
            "fix": "Scaled replicas back to 3.",
            "prevention": "Check quota headroom before raising requests or replicas.",
            "resolution": {"verified_by": "fleet-audit", "at": "2026-10-13T02:00:00Z",
                           "summary": "Finding absent on the next complete audit run."},
        }
        f = self.r.path.parent / "outcome.json"
        f.write_text(json.dumps(outcome))
        rc, out = self.r.run("from-outcome", str(f), "--write")
        self.assertTrue(out["valid"], out["problems"])
        self.assertEqual(out["id"], "LES-2026-002")
        self.assertTrue((self.r.path / out["path"]).exists())
        rc, val = self.r.run("validate")
        self.assertEqual(rc, 0, val)


class DreamTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.close()

    def test_clean_repo_is_silent(self):
        rc, out = self.r.run("dream")
        self.assertEqual(sorted(out["verified"]), ["DEC-2026-001", "LES-2026-001"])
        self.assertTrue(out["silent"], out)

    def test_unlearned_lesson_is_at_risk(self):
        self.r.write("clusters/prod-eu/payments/checkout.yaml", DEPLOY.format(mem="128Mi"))
        self.r.commit("chore: cost pass (#50)")
        rc, out = self.r.run("dream")
        self.assertEqual(out["at_risk"][0]["id"], "LES-2026-001")
        self.assertEqual(out["at_risk"][0]["introduced_by"][0]["pull_request"], "50")
        self.assertFalse(out["silent"])

    def test_revert_of_fix_is_detected(self):
        sh(self.r.path, "revert", "--no-edit", self.r.fix)
        # The revert takes checkout back to 64Mi, so the guard fires first; drop
        # the guard to see the revert detector on its own.
        p = self.r.path / "knowledge/lessons/LES-2026-001-checkout-oom.md"
        rec = dm.parse_record(p.read_text(), "x")
        rec.meta.pop("guard")
        p.write_text(dm.render_record(rec.meta, rec.body))
        self.r.commit("knowledge: drop guard")
        rc, out = self.r.run("dream")
        self.assertEqual(out["reverted"][0]["id"], "LES-2026-001")

    def test_supersession_is_reconciled_and_applied(self):
        newer = DECISION.replace("DEC-2026-001", "DEC-2026-002").replace(
            "status: active", "status: active\nsupersedes: [DEC-2026-001]").replace("832Mi", "1200Mi")
        self.r.write("knowledge/decisions/DEC-2026-002-payments-quota-v2.md", newer)
        self.r.commit("knowledge: DEC-2026-002 (#60)")
        rc, out = self.r.run("dream", "--apply")
        self.assertEqual(out["supersession"][0]["id"], "DEC-2026-001")
        old = dm.parse_record((self.r.path / "knowledge/decisions/DEC-2026-001-payments-quota.md").read_text(), "x")
        self.assertEqual((old.status, old.meta["superseded_by"]), ("superseded", "DEC-2026-002"))
        rc, val = self.r.run("validate")
        self.assertEqual(rc, 0, val)

    def test_recurrence_across_clusters(self):
        second = LESSON.format(commit="deadbeef").replace("LES-2026-001", "LES-2026-002").replace(
            "prod-eu]", "prod-us]").replace("pull/31", "pull/70")
        self.r.write("knowledge/lessons/LES-2026-002-checkout-oom-us.md", second)
        self.r.commit("knowledge: LES-2026-002 (#70)")
        rc, out = self.r.run("dream")
        keys = {g["key"]: g for g in out["recurring"]}
        self.assertIn("check:oom-kill", keys)
        self.assertEqual(keys["check:oom-kill"]["clusters"], ["prod-eu", "prod-us"])

    def test_uncovered_change_to_governed_path(self):
        self.r.write("clusters/prod-eu/payments/quota.yaml", QUOTA.format(mem="800Mi"))
        self.r.commit("payments: trim quota (#81)")
        rc, out = self.r.run("dream")
        self.assertEqual([c["pull_request"] for c in out["uncovered_changes"]], ["81"])

    def test_conflicting_active_records_are_flagged(self):
        # An accepted change and a later contradicting decision, with no
        # supersedes link: which one is the fact must be decided, not ranked.
        rival = DECISION.replace("DEC-2026-001", "DEC-2026-003").replace(
            "date: 2026-08-01", "date: 2026-10-01").replace("832Mi", "1200Mi")
        self.r.write("knowledge/decisions/DEC-2026-003-payments-quota-raise.md", rival)
        self.r.commit("knowledge: DEC-2026-003 (#95)")
        rc, out = self.r.run("dream")
        c = out["conflicts"][0]
        self.assertEqual(sorted(x["id"] for x in c["records"]), ["DEC-2026-001", "DEC-2026-003"])
        self.assertIn("newest is DEC-2026-003", c["proposal"])
        self.assertFalse(out["silent"])

    def test_obsolete_when_paths_disappear(self):
        os.remove(self.r.path / "clusters/prod-eu/payments/quota.yaml")
        self.r.commit("payments: quota moved to fleet/ (#90)")
        rc, out = self.r.run("dream")
        self.assertEqual(out["obsolete"][0]["id"], "DEC-2026-001")


class FakeHindsight(BaseHTTPRequestHandler):
    """Just enough of the Hindsight REST surface for the index path."""
    docs = {}
    calls = []

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def _send(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_PUT(self):
        FakeHindsight.calls.append(("PUT", self.path, self._body()))
        self._send({})

    def do_PATCH(self):
        FakeHindsight.calls.append(("PATCH", self.path, self._body()))
        self._send({})

    def do_POST(self):
        body = self._body()
        FakeHindsight.calls.append(("POST", self.path, body))
        if self.path.endswith("/memories"):
            for it in body["items"]:
                FakeHindsight.docs[it["document_id"]] = {
                    "id": it["document_id"],
                    "tags": it["tags"],
                    "text": it["content"],
                    "text_length": len(it["content"]),
                    "document_metadata": it.get("metadata") or {},
                }
            return self._send({"success": True})
        if self.path.endswith("/memories/recall"):
            res = [{"text": d["text"], "tags": d["tags"], "document_id": d["id"]} for d in FakeHindsight.docs.values()]
            return self._send({"results": res})
        self._send({}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.endswith("/documents"):
            q = parse_qs(u.query)
            off = int(q.get("offset", ["0"])[0])
            items = list(FakeHindsight.docs.values())[off:off + 100]
            return self._send({"items": items, "total": len(FakeHindsight.docs)})
        self._send({}, 404)

    def do_DELETE(self):
        doc = unquote(self.path.split("/documents/", 1)[1])
        FakeHindsight.docs.pop(doc, None)
        FakeHindsight.calls.append(("DELETE", doc, None))
        self._send({})


class IndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeHindsight)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeHindsight.docs, FakeHindsight.calls = {}, []
        self.r = Repo()

    def tearDown(self):
        self.r.close()

    def test_index_upserts_then_deletes_removed_records(self):
        rc, out = self.r.run("--hindsight-url", self.url, "--repo-id", "acme/gitops", "index")
        self.assertEqual(sorted(out["upserted"]), ["DEC-2026-001", "LES-2026-001"])
        retain = [c for c in FakeHindsight.calls if c[0] == "POST" and c[1].endswith("/memories")]
        item = retain[0][2]["items"][0]
        self.assertEqual(item["strategy"], "record")
        self.assertEqual(item["update_mode"], "replace")
        self.assertTrue(item["document_id"].startswith("okf:acme/gitops:knowledge/"))
        self.assertIn("scope:shared", item["tags"])
        patch = [c for c in FakeHindsight.calls if c[0] == "PATCH"][0][2]
        self.assertFalse(patch["updates"]["enable_observations"])
        # Same-length edit (e.g. changing 832Mi to 999Mi) still triggers upsert via sha256 check
        quota_rec = self.r.path / "knowledge/decisions/DEC-2026-001-payments-quota.md"
        quota_rec.write_text(quota_rec.read_text().replace("832Mi", "999Mi"))
        rc, out = self.r.run("--hindsight-url", self.url, "--repo-id", "acme/gitops", "index")
        self.assertEqual(out["upserted"], ["DEC-2026-001"])
        self.assertEqual(out["unchanged"], ["LES-2026-001"])
        os.remove(self.r.path / "knowledge/decisions/DEC-2026-001-payments-quota.md")
        self.r.commit("knowledge: retire DEC-2026-001")
        rc, out = self.r.run("--hindsight-url", self.url, "--repo-id", "acme/gitops", "index")
        self.assertEqual(out["deleted"], ["okf:acme/gitops:knowledge/decisions/DEC-2026-001-payments-quota.md"])

    def test_check_semantic_excludes_path_matches(self):
        self.r.run("--hindsight-url", self.url, "--repo-id", "acme/gitops", "index")
        sh(self.r.path, "checkout", "-q", "-b", "c")
        self.r.write("clusters/prod-eu/payments/checkout.yaml", DEPLOY.format(mem="64Mi"))
        self.r.commit("x")
        rc, out = self.r.run("--hindsight-url", self.url, "check", "--base", "main", "--semantic")
        self.assertEqual([s["id"] for s in out["semantic"]], ["DEC-2026-001"])


class MultiForgeTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.close()

    def test_parse_remote_url_identity_supports_github_gitlab_bitbucket(self):
        self.assertEqual(dm._parse_remote_url_identity("https://github.com/acme/gitops.git"), "acme/gitops")
        self.assertEqual(dm._parse_remote_url_identity("git@gitlab.com:acme/platform/infra/gitops.git"), "acme/platform/infra/gitops")
        self.assertEqual(dm._parse_remote_url_identity("https://gitlab.example.com/group/sub/repo"), "group/sub/repo")
        self.assertEqual(dm._parse_remote_url_identity("https://bitbucket.example.com/scm/proj/gitops.git"), "proj/gitops")
        self.assertEqual(dm._parse_remote_url_identity("acme/sub/gitops"), "acme/sub/gitops")

    def test_repo_identity_reads_vcs_session_when_remote_stripped(self):
        vcs_root = self.r.path / "vcs-scratch"
        sessions_dir = vcs_root / ".sessions"
        sessions_dir.mkdir(parents=True)
        worktree = vcs_root / "work-123"
        worktree.mkdir()
        (sessions_dir / "s1.json").write_text(json.dumps({
            "path": str(worktree),
            "repo": "https://gitlab.com/acme/sub/gitops.git",
        }))
        old_env = os.environ.get("KUBE_AGENTS_VCS_ROOT")
        os.environ["KUBE_AGENTS_VCS_ROOT"] = str(vcs_root)
        try:
            self.assertEqual(dm.repo_identity(worktree), "acme/sub/gitops")
        finally:
            if old_env is None:
                os.environ.pop("KUBE_AGENTS_VCS_ROOT", None)
            else:
                os.environ["KUBE_AGENTS_VCS_ROOT"] = old_env

    def test_pr_ref_matches_github_gitlab_and_bitbucket_merge_commits(self):
        self.assertEqual(dm._match_pr_num("feat: bump mem (#42)"), "42")
        self.assertEqual(dm._match_pr_num("Merge pull request #17 from acme/fix"), "17")
        self.assertEqual(dm._match_pr_num("fix: quota (!88)"), "88")
        self.assertEqual(dm._match_pr_num("Merge branch 'fix'\n\nSee merge request acme/sub/gitops!104"), "104")
        self.assertEqual(dm._match_pr_num("Merged in feature/foo (pull request #53)"), "53")

    def test_bootstrap_supports_gitlab_merge_requests_and_vcs_client_schema(self):
        # Create a GitLab-style commit on the repo so _git_commit_index can link it
        self.r.write("clusters/prod-eu/payments/checkout.yaml", DEPLOY.format(mem="64Mi"))
        self.r.commit("feat(payments): lower checkout memory to 64Mi (!10)")
        self.r.write("clusters/prod-eu/payments/checkout.yaml", DEPLOY.format(mem="256Mi"))
        self.r.commit("Revert \"feat(payments): lower checkout memory to 64Mi\"\n\nSee merge request acme/gitops!12")

        # Normalized vcs_client proposal-list / issue-list schema
        prs = [
            {
                "number": 10,
                "title": "feat(payments): lower checkout memory to 64Mi",
                "state": "merged",
                "url": "https://gitlab.com/acme/gitops/-/merge_requests/10",
                "body": "Cut memory request.",
            },
            {
                "number": 12,
                "title": "revert(payments): restore checkout memory to 256Mi",
                "state": "merged",
                "url": "https://gitlab.com/acme/gitops/-/merge_requests/12",
                "body": "Reverts !10\nFixes #11\n64Mi caused OOMKilled (ExitCode 137) under load.",
            },
            {
                "number": 15,
                "title": "feat(payments): raise namespace quota to 64Gi",
                "state": "closed",
                "url": "https://gitlab.com/acme/gitops/-/merge_requests/15",
                "body": "Raise namespace quota.",
                "diff": "--- a/clusters/prod-eu/payments/namespace-quota.yaml\n+++ b/clusters/prod-eu/payments/namespace-quota.yaml\n@@ -1 +1 @@\n",
                "_comments": [{"user": "sec-reviewer", "body": "Rejected: 32Gi is our multi-tenant ceiling."}],
            },
            {
                "number": 16,
                "title": "fix(payments): right-size batch worker after rejection of MR !15",
                "state": "merged",
                "url": "https://gitlab.com/acme/gitops/-/merge_requests/16",
                "body": "Follows rejection of MR !15\nFixes #14\nRight-size worker instead of raising namespace quota.",
            },
        ]
        issues = [
            {
                "number": 11,
                "title": "incident: checkout OOMKilled after !10",
                "state": "closed",
                "body": "Pod crashed with ExitCode 137 after merge request !10.",
            },
            {
                "number": 14,
                "title": "incident: payments quota exceeded",
                "state": "closed",
                "body": "Batch worker exceeded namespace memory quota.",
            },
        ]
        prs_file = self.r.path / "gitlab_prs.json"
        issues_file = self.r.path / "gitlab_issues.json"
        prs_file.write_text(json.dumps(prs))
        issues_file.write_text(json.dumps(issues))

        rc, out = self.r.run(
            "bootstrap",
            "--prs-json", str(prs_file),
            "--issues-json", str(issues_file),
            "--write",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["triage"]["revert-lesson"], 1)
        self.assertEqual(out["triage"]["decision"], 1)
        records = {rec.id: rec for rec in dm.load_records(self.r.path)}
        self.assertIn("LES-2026-002", records)
        les = records["LES-2026-002"]
        self.assertIn("clusters/prod-eu/payments/checkout.yaml", les.scope["paths"])
        self.assertIn("!12", str(les.evidence.get("pull_request", "")))
        self.assertIn("!10", str(les.evidence.get("breaking_pr", "")))
        self.assertIn("DEC-2026-002", records)
        dec = records["DEC-2026-002"]
        self.assertIn("clusters/prod-eu/payments/namespace-quota.yaml", dec.scope["paths"])
        self.assertIn("!15", str(dec.evidence.get("breaking_pr", "")))


class FakeLLM(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible /v1/chat/completions mock for OKF synthesis tests."""
    responses = []
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        FakeLLM.calls.append(body)
        reply = FakeLLM.responses.pop(0) if FakeLLM.responses else {}
        payload = {"choices": [{"message": {"content": json.dumps(reply)}}]}
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class LLMSynthesisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.llm_url = f"http://127.0.0.1:{cls.srv.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeLLM.responses, FakeLLM.calls = [], []
        self.r = Repo()

    def tearDown(self):
        self.r.close()

    def test_from_outcome_synthesizes_okf_and_verifies_guard_against_head(self):
        # Valid guard that passes against HEAD (checkout.yaml has 256Mi on container[name=app])
        FakeLLM.responses.append({
            "title": "Keep checkout memory request at 256Mi or higher",
            "root_cause": "Lowering checkout memory below 256Mi triggered ExitCode 137 under load.",
            "fix": "Restored resources.requests.memory to 256Mi in PR #44.",
            "verification": "Zero OOMKills observed for 24 hours after rollout.",
            "prevention": "Enforce a minimum memory request of 256Mi on checkout.",
            "guard": {
                "rule": "Keep checkout memory request >= 256Mi.",
                "severity": "block",
                "expect": {
                    "file": "clusters/prod-eu/payments/checkout.yaml",
                    "path": "spec.template.spec.containers[name=app].resources.requests.memory",
                    "op": ">=",
                    "value": "256Mi",
                },
            },
        })
        outcome = {
            "pull_request": "https://github.com/acme/gitops/pull/44",
            "title": "raw pr title",
            "merged_at": "2026-10-12T10:00:00Z",
            "merged_by": "bob",
            "commit": "abc123",
            "files": ["clusters/prod-eu/payments/checkout.yaml"],
            "cluster": "prod-eu",
            "pr_body": "Messy PR description about fixing checkout memory.",
        }
        f = self.r.path.parent / "outcome_llm.json"
        f.write_text(json.dumps(outcome))
        rc, out = self.r.run("from-outcome", str(f), "--llm-url", self.llm_url, "--write")
        self.assertEqual(rc, 0)
        self.assertTrue(out["llm_synthesized"])
        self.assertTrue(out["valid"], out["problems"])
        rec = dm.parse_record((self.r.path / out["path"]).read_text(), out["path"])
        self.assertEqual(rec.title, "Keep checkout memory request at 256Mi or higher")
        self.assertEqual(rec.guard["expect"]["value"], "256Mi")

    def test_from_outcome_drops_hallucinated_guard_that_fails_on_head(self):
        # Hallucinated guard expecting 1Gi (fails on HEAD where checkout.yaml has 256Mi)
        FakeLLM.responses.append({
            "title": "Synthesized lesson with bad guard",
            "root_cause": "Root cause text.",
            "fix": "Fix text.",
            "verification": "Verified.",
            "prevention": "Prevention text.",
            "guard": {
                "rule": "Hallucinated 1Gi requirement.",
                "severity": "block",
                "expect": {
                    "file": "clusters/prod-eu/payments/checkout.yaml",
                    "path": "spec.template.spec.containers[name=app].resources.requests.memory",
                    "op": ">=",
                    "value": "1Gi",
                },
            },
        })
        outcome = {
            "pull_request": "https://github.com/acme/gitops/pull/45",
            "title": "raw title",
            "merged_at": "2026-10-12T10:00:00Z",
            "files": ["clusters/prod-eu/payments/checkout.yaml"],
        }
        f = self.r.path.parent / "outcome_bad_guard.json"
        f.write_text(json.dumps(outcome))
        rc, out = self.r.run("from-outcome", str(f), "--llm-url", self.llm_url, "--write")
        self.assertEqual(rc, 0)
        self.assertTrue(out["llm_synthesized"])
        rec = dm.parse_record((self.r.path / out["path"]).read_text(), out["path"])
        self.assertEqual(rec.guard, {})

    def test_resolve_llm_config_reads_litellm_gateway_from_agent_config_yaml(self):
        cfg_dir = self.r.path / "agent-home"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.yaml").write_text(
            "model:\n"
            "  provider: custom\n"
            "  model: model-default\n"
            f"  base_url: {self.llm_url}\n"
            "  api_key: none\n"
        )
        old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(cfg_dir)
        try:
            ns = dm.argparse.Namespace(no_llm=False, llm_url=None, llm_model=None)
            base, model, key = dm._resolve_llm_config(ns)
            self.assertEqual(base, self.llm_url)
            self.assertEqual(model, "model-default")
            self.assertEqual(key, "none")
        finally:
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home


@unittest.skipUnless(os.environ.get("DECISION_MEMORY_LIVE_URL"), "set DECISION_MEMORY_LIVE_URL for a live run")
class LiveHindsightTests(unittest.TestCase):
    def test_round_trip(self):
        url = os.environ["DECISION_MEMORY_LIVE_URL"]
        r = Repo()
        try:
            bank = "dm-live-test"
            rc, out = r.run("--hindsight-url", url, "--bank", bank, "--repo-id", "acme/gitops", "index", "--force")
            self.assertEqual(len(out["upserted"]), 2, out)
            hs = dm.Hindsight(url, bank)
            hits = hs.recall("why must checkout keep 256Mi memory", tag_groups=[
                {"tags": ["scope:shared"], "match": "all_strict"},
                {"tags": ["kind:lesson"], "match": "all_strict"}])
            self.assertTrue(any("LES-2026-001" in h.get("text", "") for h in hits), hits)
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()
