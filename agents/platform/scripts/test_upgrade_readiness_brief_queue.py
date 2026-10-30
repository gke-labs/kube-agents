"""Tests for upgrade_readiness_brief_queue.py: the queue read the weekly brief's turn cannot make."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

import upgrade_readiness_brief_queue as brief_queue  # noqa: E402
import upgrade_readiness_review as review  # noqa: E402

ROSTER = Path(__file__).resolve().parents[1] / "cron" / "jobs.json"
PROJECT = "acme-prod"
OTHER_PROJECT = "acme-dev"


def row(fid, check="upgrade-pdb", state="queued", project=PROJECT, severity="critical", **extra) -> dict:
    return {
        "id": fid,
        "check": check,
        "state": state,
        "severity": severity,
        "project": project,
        "cluster": "seeded-b",
        "namespace": "seeded-upgrade",
        "object": "pinned-batch-runner",
        "title": "Budget pinned-batch-runner refuses eviction of its only pod",
        "recommendation": {"action": "Raise maxUnavailable or add a replica.", "rationale": "the drain evicts the only pod", "risk": "an outage"},
        "detail": "the rule's full text",
        "rubric": {"impact": 3},
        **extra,
    }


class FakeQueue:
    def __init__(self, ranked, decided=None):
        self.ranked = ranked
        self.decided = decided or {}
        self.paths = []
        self.queries = []

    def __call__(self, path, body=None):
        assert body is None, "the brief's read registers nothing"
        self.paths.append(path)
        if path == brief_queue.RANKED_PATH:
            return {"findings": self.ranked}
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(path).query))
        self.queries.append(query)
        assert query["limit"] == str(brief_queue.PAGE_LIMIT), query
        # The queue narrows the page to the family before it cuts it, across every project.
        rows = [r for (state, _), rows in self.decided.items() if state == query["state"] for r in rows]
        return {"findings": [r for r in rows if r["check"].startswith(query["check_prefix"])][: brief_queue.PAGE_LIMIT]}


class TheQueueView(unittest.TestCase):
    def run_main(self, queue):
        out = io.StringIO()
        with mock.patch.object(review, "kv_request", queue), redirect_stdout(out):
            code = brief_queue.main()
        return code, json.loads(out.getvalue())

    def test_only_upgrade_rows_reach_the_turn_with_the_fields_it_composes_from(self):
        queue = FakeQueue(
            [row("upgrade-pdb:a"), row("cost:b", check="cost-idle-node"), row("upgrade-webhook:c", check="upgrade-webhook", severity="major")],
            {("dismissed", PROJECT): [row("upgrade-pdb:d", state="dismissed"), row("cost:e", check="cost-idle-node", state="dismissed")]},
        )
        code, view = self.run_main(queue)
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in view["open"]], ["upgrade-pdb:a", "upgrade-webhook:c"])
        # An open critical may become a Decide-now line from the queue alone: it carries what the line names.
        self.assertEqual(set(view["open"][0]), set(brief_queue.DETAIL_FIELDS) | {"action"})
        self.assertEqual(view["open"][0]["action"], "Raise maxUnavailable or add a replica.")
        # Every other row is there for its state, without the text a line would quote.
        self.assertEqual(set(view["open"][1]), {"id", "state", "severity", "check", "project", "cluster"})
        self.assertEqual([r["id"] for r in view["dismissed"]], ["upgrade-pdb:d"])
        self.assertEqual(set(view["dismissed"][0]), {"id", "state", "severity", "check", "project", "cluster"})
        self.assertEqual((view["snoozed"], view["cut"], view["unread"]), ([], [], []))

    def test_each_decided_state_is_one_page_of_the_family_across_every_project(self):
        # No project list is needed: a project whose rows are all decided, or one a summary
        # spells by number, is counted all the same.
        queue = FakeQueue(
            [row("upgrade-pdb:a")],
            {("snoozed", OTHER_PROJECT): [row("upgrade-pdb:s", state="snoozed", project=OTHER_PROJECT, snoozed_until="2026-11-01T00:00:00Z")]},
        )
        code, view = self.run_main(queue)
        self.assertEqual(code, 0)
        self.assertEqual(queue.queries, [{"state": s, "check_prefix": review.CHECK_PREFIX, "limit": str(brief_queue.PAGE_LIMIT)} for s in brief_queue.DECIDED_STATES])
        self.assertEqual(view["snoozed"], [{"id": "upgrade-pdb:s", "state": "snoozed", "severity": "critical", "check": "upgrade-pdb", "project": OTHER_PROJECT, "cluster": "seeded-b", "snoozed_until": "2026-11-01T00:00:00Z"}])

    def test_an_accepted_critical_carries_its_state_alone(self):
        code, view = self.run_main(FakeQueue([row("upgrade-pdb:a", state="accepted")]))
        self.assertEqual(set(view["open"][0]), {"id", "state", "severity", "check", "project", "cluster"})

    def test_a_full_page_of_the_family_is_named_cut(self):
        full = [row(f"upgrade-pdb:{n}", state="dismissed") for n in range(brief_queue.PAGE_LIMIT)]
        code, view = self.run_main(FakeQueue([row("upgrade-pdb:a")], {("dismissed", PROJECT): full}))
        self.assertEqual(view["cut"], [{"state": "dismissed"}])
        self.assertEqual(len(view["dismissed"]), brief_queue.PAGE_LIMIT)

    def test_a_decided_page_it_cannot_read_costs_only_that_count(self):
        queue = FakeQueue([row("upgrade-pdb:a")])
        answer = queue.__call__

        def flaky(path, body=None):
            if "state=dismissed" in path:
                raise OSError("HTTP 503 Service Unavailable")
            return answer(path, body)

        code, view = self.run_main(flaky)
        self.assertEqual(code, 0)
        self.assertNotIn("error", view)
        self.assertEqual([r["id"] for r in view["open"]], ["upgrade-pdb:a"])
        self.assertEqual(view["unread"], [{"state": "dismissed", "error": "HTTP 503 Service Unavailable"}])

    def test_a_queue_it_cannot_read_is_said_and_the_turn_still_runs(self):
        def refuse(path, body=None):
            raise OSError("HTTP 503 Service Unavailable")

        code, view = self.run_main(refuse)
        self.assertEqual(code, 0)
        self.assertEqual(view, {"error": brief_queue.ERROR_PREFIX + "HTTP 503 Service Unavailable"})

    def test_an_answer_without_a_findings_list_is_a_failed_read(self):
        code, view = self.run_main(lambda path, body=None: {"detail": "not found"})
        self.assertEqual(code, 0)
        self.assertIn("without a findings list", view["error"])

    def test_the_read_is_a_get_with_the_queue_token(self):
        seen = {}

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def answer(req, timeout):
            seen.update(method=req.get_method(), data=req.data, auth=req.get_header("Authorization"), url=req.full_url)
            return Answer(b'{"findings": []}')

        with mock.patch.dict(os.environ, {review.KV_AUTH_VARIABLE: "t0ken"}), mock.patch.object(review.urllib.request, "urlopen", answer):
            self.assertEqual(review.kv_request(brief_queue.RANKED_PATH), {"findings": []})
        self.assertEqual((seen["method"], seen["data"], seen["auth"]), ("GET", None, "Bearer t0ken"))
        self.assertTrue(seen["url"].endswith("/v1/findings/ranked"))


class TheBriefEntry(unittest.TestCase):
    def test_the_brief_turn_holds_the_terminal_alone_and_reads_the_queue_before_it(self):
        jobs = {job["id"]: job for job in json.loads(ROSTER.read_text())["jobs"]}
        brief = jobs[review.BRIEF_JOB_ID]
        self.assertEqual(brief["script"], Path(brief_queue.__file__).name)
        toolsets = set(brief["enabled_toolsets"])
        # The sentinel drops every MCP server, platform_control and its update_finding among them.
        # Anything beside the terminal widens the turn: a named server survives the sentinel, "all"
        # brings the MCP toolsets back, kanban files a card that asks for a decision, and the file
        # toolset's writes are not held to the risk: high command policy.
        self.assertIn("no_mcp", toolsets)
        self.assertIn("terminal", toolsets, "the turn reads the SOP and the summary with cat")
        self.assertLessEqual(toolsets, {"terminal", "no_mcp"})


if __name__ == "__main__":
    unittest.main()
