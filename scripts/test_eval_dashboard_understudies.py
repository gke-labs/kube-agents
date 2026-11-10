"""understudies.py: the per-domain scorecard (#3968 sections 2 and 6d), on
coverage_gaps' synthetic checkout: alpha covered by alpha-seat, beta and
gamma on the allowlist."""

import contextlib
import datetime
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
import unittest.mock

import yaml
from eval_dashboard import understudies
from test_eval_dashboard_coverage_gaps import NOW, WORKFLOW, Harness, data_doc
from test_eval_dashboard_nightly_bugs import FakeGh

STEP_NAME = "Rank each domain's understudies for the scorecard"
RENDER_STEP_NAME = "Render and publish the dashboard"


def red_issue(number, slug, created="2026-10-20T13:00:00Z"):
    labels = [{"name": "nightly-red"}] + ([{"name": f"domain:{slug}"}] if slug else [{"name": "Infra"}])
    return {"number": number, "title": f"Nightly: case-{number}", "body": "", "labels": labels, "created_at": created, "comments": 0}


def would_admit_store(cases):
    """store_records' answer: ``{case: record}``."""
    return {c: {"state": "would-admit", "runs": 21, "passes": 21} for c in cases}


class ScorecardTest(Harness):
    def card(self, store=None, red_issues=None, doc=None):
        return understudies.scorecard(self.root, doc or data_doc(), store, datetime.datetime.fromisoformat(NOW), red_issues)

    def rows(self, **kwargs):
        return {d["slug"]: d for d in self.card(**kwargs)["domains"]}

    def test_one_row_per_domain_in_file_order_with_its_seat(self):
        doc = self.card()
        self.assertEqual([d["slug"] for d in doc["domains"]], ["alpha", "beta", "gamma"])
        alpha, beta, gamma = doc["domains"]
        self.assertEqual((alpha["seats"], alpha["days_uncovered"], alpha["owner"]), (["alpha-seat"], None, "alice"))
        self.assertEqual((beta["seats"], beta["days_uncovered"], beta["allowlist_issue"]), ([], 9, 3120))
        self.assertEqual((gamma["owner"], gamma["held_out"], gamma["uncovered_since"]), ("grace", "gamma-held", "2026-10-21"), "the record's own owner wins, as on the issue")
        self.assertEqual(doc["bar"], {"rate": 0.9, "min_runs": 20, "nights": 14})

    def test_understudies_are_the_best_two_candidates(self):
        beta = self.rows()["beta"]
        self.assertEqual([u["case"] for u in beta["understudies"]], ["beta-good", "beta-young"], "coverage_gaps' ranking, cut to two")
        self.assertEqual(beta["candidates"], 3)
        self.assertEqual((beta["understudies"][0]["passed"], beta["understudies"][0]["graded"], beta["understudies"][0]["meets_bar"]), (21, 21, True))

    def test_a_covered_domain_gets_understudies_too(self):
        (self.root / "bench" / "tasks" / "alpha-spare").mkdir(parents=True)
        (self.root / "bench" / "tasks" / "alpha-spare" / "task.yaml").write_text("name: alpha-spare\ndomain: alpha\nverification_spec:\n  - type: reply_contains\n    phrases: [x]\n")
        doc = data_doc()
        doc["cases"].append({"name": "alpha-spare", "domain": "alpha", "active": False, "nightly_active": True})
        self.assertEqual([u["case"] for u in self.rows(doc=doc)["alpha"]["understudies"]], ["alpha-spare"])
        self.assertEqual(self.rows()["alpha"]["understudies"], [], "no candidate: an empty list, not unread")

    def test_ready_is_a_would_admit_store_verdict_and_unknown_without_a_store(self):
        self.assertTrue(all(d["understudy_ready"] is None for d in self.card()["domains"]), "no store read: not known")
        with unittest.mock.patch.object(understudies.coverage_gaps, "store_records", return_value=would_admit_store(["beta-good"])):
            rows = self.rows(store={})
        self.assertEqual((rows["alpha"]["understudy_ready"], rows["beta"]["understudy_ready"], rows["gamma"]["understudy_ready"]), (False, True, False))
        self.assertEqual(rows["beta"]["understudies"][0]["store"], {"state": "would-admit", "runs": 21, "passes": 21})
        self.assertEqual(rows["gamma"]["understudies"][0]["store"], {"state": "none", "runs": None, "passes": None}, "read, but no record at the key")

    def test_nightly_red_counts_by_domain_label_with_the_oldest_age(self):
        issues = [red_issue(1, "beta", "2026-10-20T13:00:00Z"), red_issue(2, "beta", "2026-10-24T13:00:00Z"), red_issue(3, None), red_issue(4, "alpha")]
        rows = self.rows(red_issues=issues)
        self.assertEqual(rows["beta"]["nightly_red"], {"open": 2, "oldest_days": 6})
        self.assertEqual(rows["alpha"]["nightly_red"], {"open": 1, "oldest_days": 6})
        self.assertEqual(rows["gamma"]["nightly_red"], {"open": 0, "oldest_days": None})
        self.assertIsNone(self.rows()["beta"]["nightly_red"], "not read: null, never zero")

    def test_an_issue_newer_than_now_is_zero_days_old(self):
        rows = self.rows(red_issues=[red_issue(1, "beta", "2026-10-26T13:00:05Z")])
        self.assertEqual(rows["beta"]["nightly_red"], {"open": 1, "oldest_days": 0}, "a --now in the past or clock skew never reads -1 days")

    def test_no_cases_in_data_json_reads_as_not_read(self):
        doc = data_doc()
        doc["cases"] = []
        beta = self.rows(doc=doc)["beta"]
        self.assertIsNone(beta["understudies"])
        self.assertIsNone(beta["understudy_ready"])


class MainTest(Harness):
    def run_main(self, gh=None, extra=()):
        data = self.dir / "data.json"
        data.write_text(json.dumps(data_doc()))
        out = self.dir / "scorecard.json"
        argv = ["--data", str(data), "--out", str(out), "--repo-root", str(self.root), "--now", NOW, *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = understudies.main(argv, gh_runner=gh)
        return rc, out, stdout.getvalue(), stderr.getvalue()

    def test_writes_the_scorecard_and_nothing_to_github(self):
        gh = FakeGh(open_issues=[red_issue(1, "beta")])
        rc, out, stdout, stderr = self.run_main(gh)
        self.assertEqual(rc, 0, stderr)
        doc = json.loads(out.read_text())
        self.assertTrue(doc["nightly_red_read"])
        self.assertEqual({d["slug"]: d["nightly_red"]["open"] for d in doc["domains"]}, {"alpha": 0, "beta": 1, "gamma": 0})
        self.assertEqual(gh.writes(), [], "a read and nothing else")
        self.assertIn("beta: seat none; understudies beta-good, beta-young (store not read); nightly-red 1", stdout)

    def test_an_untrusted_listing_is_not_read(self):
        rc, out, _, stderr = self.run_main(FakeGh(open_issues=[red_issue(1, "beta")], empty_open=True))
        self.assertEqual(rc, 0, stderr)
        doc = json.loads(out.read_text())
        self.assertFalse(doc["nightly_red_read"])
        self.assertTrue(all(d["nightly_red"] is None for d in doc["domains"]))

    def test_no_github_reads_nothing(self):
        gh = FakeGh()
        rc, out, _, _ = self.run_main(gh, ["--no-github"])
        self.assertEqual((rc, gh.calls), (0, []))
        self.assertFalse(json.loads(out.read_text())["nightly_red_read"])

    def test_a_bad_read_writes_no_scorecard(self):
        self.roster(["beta-good"])  # alpha now uncovered and off the allowlist
        rc, out, _, stderr = self.run_main(FakeGh(), ["--no-github"])
        self.assertEqual(rc, 1)
        self.assertFalse(out.exists())
        self.assertIn("no scorecard written", stderr)


class WorkflowTest(unittest.TestCase):
    """ci-health.yml: a best-effort, read-only step before the render, which
    passes the scorecard on only when it was written."""

    def steps(self):
        return yaml.safe_load(WORKFLOW.read_text())["jobs"]["refresh-and-adjudicate"]["steps"]

    def step(self, name):
        [step] = [s for s in self.steps() if s.get("name") == name]
        return step

    def test_best_effort_bounded_read_only_and_before_the_render(self):
        step = self.step(STEP_NAME)
        names = [s.get("name") for s in self.steps()]
        self.assertTrue(step.get("continue-on-error"))
        self.assertLessEqual(step.get("timeout-minutes"), 3)
        self.assertLess(names.index(STEP_NAME), names.index(RENDER_STEP_NAME))
        self.assertEqual(step["env"], {"KUBE_AGENTS_ISSUES_REPO": "${{ vars.KUBE_AGENTS_ISSUES_REPO }}", "GH_TOKEN": "${{ steps.issues_token.outputs.token || github.token }}"},
                         "the issues repository and its token, the workflow's own when unset (tests/test_ci_health_issues_repo.py)")
        self.assertNotIn("--live", step["run"])

    def run_step(self, name, files):
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "bin" / "python3"
            fake.parent.mkdir()
            fake.write_text('#!/bin/sh\necho "$@"\n')
            fake.chmod(0o755)
            (pathlib.Path(tmp) / "work").mkdir()
            for f in files:
                (pathlib.Path(tmp) / "work" / f).write_text("{}")
            env = {"PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}", "DASHBOARD_BUCKET": "gs://bucket"}
            proc = subprocess.run(["bash", "-e", "-c", self.step(name)["run"]], env=env, cwd=tmp, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_the_store_and_the_scorecard_are_passed_only_when_present(self):
        self.assertNotIn("--store", self.run_step(STEP_NAME, []))
        self.assertIn("--store work/store.json", self.run_step(STEP_NAME, ["store.json"]))
        self.assertNotIn("--scorecard", self.run_step(RENDER_STEP_NAME, ["health.json"]))
        self.assertIn("--scorecard work/scorecard.json", self.run_step(RENDER_STEP_NAME, ["health.json", "scorecard.json"]))


if __name__ == "__main__":
    unittest.main()
