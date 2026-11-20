"""reconcile_lease_predecessor.py: fail-closed check that a held lease is a finished reconcile's (#6381)."""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import unittest
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_lease_predecessor", _ROOT / "scripts" / "release" / "reconcile_lease_predecessor.py")
pred = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pred)

ENV = "autopush"
NOTE = "scheduled reconcile of autopush"


def held(session="gha-44371705693", note=NOTE):
    return [{"name": "autopush", "state": "held", "session": session, "note": note,
             "holder": "runner@runnervm", "expiresAt": "2026-11-18T20:09:42Z"}]


def run(status="completed", conclusion="success", path=".github/workflows/autopush-deploy.yml",
        run_id="44371705693"):
    return {"id": int(run_id), "status": status, "conclusion": conclusion, "path": path}


class Decide(unittest.TestCase):
    def test_a_finished_reconcile_runs_lease_is_taken_over(self):
        d = pred.decide(held(), ENV, "gke-labs/kube-agents", lambda r, i: run())
        self.assertEqual(d, {"takeover": True, "session": "gha-44371705693", "run_id": "44371705693",
                             "conclusion": "success", "path": ".github/workflows/autopush-deploy.yml"})

    def test_a_cancelled_or_failed_run_is_finished_too(self):
        for conclusion in ("cancelled", "failure", "timed_out"):
            with self.subTest(conclusion=conclusion):
                d = pred.decide(held(), ENV, "r", lambda r, i: run(conclusion=conclusion))
                self.assertTrue(d["takeover"])
                self.assertEqual(d["conclusion"], conclusion)

    def test_a_run_still_going_is_refused(self):
        for status in ("in_progress", "queued", "waiting", "pending"):
            with self.subTest(status=status):
                d = pred.decide(held(), ENV, "r", lambda r, i: run(status=status, conclusion=None))
                self.assertFalse(d["takeover"])
                self.assertIn("still", d["reason"])

    def test_a_person_or_agent_session_is_refused_without_asking_github(self):
        fetch = mock.Mock()
        for session in ("12345", "claude-abc", "nopid-1", "gha-", "gha-1x", "", "xgha-1"):
            with self.subTest(session=session):
                d = pred.decide(held(session=session), ENV, "r", fetch)
                self.assertFalse(d["takeover"])
        fetch.assert_not_called()

    def test_another_note_is_refused(self):
        for note in ("scheduled reconcile of staging", "live test of PR 6093", ""):
            with self.subTest(note=note):
                d = pred.decide(held(note=note), ENV, "r", lambda r, i: run())
                self.assertFalse(d["takeover"])

    def test_a_run_of_another_workflow_is_refused(self):
        d = pred.decide(held(), ENV, "r", lambda r, i: run(path=".github/workflows/ci.yml"))
        self.assertFalse(d["takeover"])
        self.assertIn("not a reconcile workflow", d["reason"])

    def test_every_reconcile_caller_is_listed(self):
        callers = {
            ".github/workflows/" + p.name
            for p in (_ROOT / ".github" / "workflows").glob("*.yml")
            if "uses: ./.github/workflows/reconcile-environment.yml" in p.read_text()
        }
        callers.add(".github/workflows/reconcile-environment.yml")
        callers.discard(".github/workflows/drift-detect.yml")  # plan only: takes no lease
        self.assertEqual(callers, set(pred.RECONCILE_WORKFLOWS))

    def test_any_api_trouble_is_refused(self):
        def boom(r, i):
            raise RuntimeError("HTTP 403: Resource not accessible by integration")
        for fetch in (boom, lambda r, i: {}, lambda r, i: run(run_id="1"), lambda r, i: "nope"):
            with self.subTest(fetch=fetch):
                d = pred.decide(held(), ENV, "r", fetch)
                self.assertFalse(d["takeover"])

    def test_not_exactly_one_held_lease_is_refused(self):
        self.assertFalse(pred.decide([], ENV, "r", lambda r, i: run())["takeover"])
        self.assertFalse(pred.decide(held() + held(), ENV, "r", lambda r, i: run())["takeover"])
        self.assertFalse(pred.decide([{"state": "free"}], ENV, "r", lambda r, i: run())["takeover"])


class Cli(unittest.TestCase):
    def _main(self, stdin, argv=("--env", ENV, "--repo", "gke-labs/kube-agents")):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("sys.stdout", out):
            rc = pred.main(list(argv))
        return rc, json.loads(out.getvalue())

    def test_unreadable_status_is_no(self):
        rc, d = self._main("not json")
        self.assertEqual(rc, 0)
        self.assertFalse(d["takeover"])

    def test_no_repository_is_no(self):
        rc, d = self._main(json.dumps(held()), argv=("--env", ENV, "--repo", ""))
        self.assertFalse(d["takeover"])

    def test_the_gh_call_is_the_run_endpoint(self):
        with mock.patch.object(pred.subprocess, "run") as sp:
            sp.return_value = mock.Mock(returncode=0, stdout=json.dumps(run()), stderr="")
            rc, d = self._main(json.dumps(held()))
        self.assertTrue(d["takeover"])
        self.assertEqual(sp.call_args[0][0], ["gh", "api", "repos/gke-labs/kube-agents/actions/runs/44371705693"])

    def test_a_failing_gh_call_is_no(self):
        with mock.patch.object(pred.subprocess, "run") as sp:
            sp.return_value = mock.Mock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
            rc, d = self._main(json.dumps(held()))
        self.assertFalse(d["takeover"])
        self.assertIn("404", d["reason"])


if __name__ == "__main__":
    unittest.main()
