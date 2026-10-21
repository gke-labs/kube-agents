"""Tests for upgrade_readiness_brief_queue.py: the queue read the weekly brief's turn cannot make."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import upgrade_readiness_brief_queue as brief_queue  # noqa: E402
import upgrade_readiness_review as review  # noqa: E402
import upgrade_readiness_watch as watch  # noqa: E402

ROSTER = Path(__file__).resolve().parents[1] / "cron" / "jobs.json"
PLATFORM_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
SHARED_CONFIG = Path(__file__).resolve().parents[3] / "deploy" / "shared" / "defaults" / "config.yaml"
PROJECT = "acme-prod"
OTHER_PROJECT = "acme-dev"


def row(fid, check="upgrade-pdb", state="queued", project=PROJECT, **extra) -> dict:
    return {
        "id": fid,
        "check": check,
        "state": state,
        "severity": "critical",
        "project": project,
        "cluster": "seeded-b",
        "namespace": "seeded-upgrade",
        "object": "pinned-batch-runner",
        "title": "Budget pinned-batch-runner refuses eviction of its only pod",
        "recommendation": "Raise maxUnavailable or add a replica.",
        "detail": "the rule's full text",
        "rubric": {"impact": 3},
        **extra,
    }


class FakeQueue:
    def __init__(self, ranked, decided=None):
        self.ranked = ranked
        self.decided = decided or {}
        self.paths = []

    def __call__(self, path, body=None):
        assert body is None, "the brief's read registers nothing"
        self.paths.append(path)
        if path == brief_queue.RANKED_PATH:
            return {"findings": self.ranked}
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(path).query))
        self.assertLimit(query)
        return {"findings": self.decided.get((query["state"], query["project"]), [])}

    @staticmethod
    def assertLimit(query):
        assert query["limit"] == str(brief_queue.PAGE_LIMIT), query


class TheQueueView(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {watch.HOME_ENV: self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_summary(self, projects):
        weekly = review.weekly_home()
        weekly.mkdir(parents=True, exist_ok=True)
        (weekly / review.LATEST_JSON_NAME).write_text(json.dumps({"projects": projects}))

    def run_main(self, queue):
        out = io.StringIO()
        with mock.patch.object(review, "kv_request", queue), redirect_stdout(out):
            code = brief_queue.main()
        return code, json.loads(out.getvalue())

    def test_only_upgrade_rows_reach_the_turn_with_the_fields_it_composes_from(self):
        queue = FakeQueue(
            [row("upgrade-pdb:a"), row("cost:b", check="cost-idle-node"), row("upgrade-webhook:c", check="upgrade-webhook", state="accepted")],
            {("dismissed", PROJECT): [row("upgrade-pdb:d", state="dismissed"), row("cost:e", check="cost-idle-node", state="dismissed")]},
        )
        code, view = self.run_main(queue)
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in view["open"]], ["upgrade-pdb:a", "upgrade-webhook:c"])
        self.assertEqual(set(view["open"][0]), set(brief_queue.OPEN_FIELDS))
        self.assertEqual([r["id"] for r in view["dismissed"]], ["upgrade-pdb:d"])
        self.assertEqual(set(view["dismissed"][0]), {"id", "severity", "project", "cluster"})
        self.assertEqual(view["snoozed"], [])
        self.assertEqual(view["cut"], [])

    def test_each_project_the_summary_or_the_backlog_names_is_read_by_state(self):
        self.write_summary([OTHER_PROJECT])
        queue = FakeQueue(
            [row("upgrade-pdb:a")],
            {("snoozed", OTHER_PROJECT): [row("upgrade-pdb:s", state="snoozed", project=OTHER_PROJECT, snoozed_until="2026-11-01T00:00:00Z")]},
        )
        code, view = self.run_main(queue)
        self.assertEqual(view["projects"], [OTHER_PROJECT, PROJECT])
        asked = sorted((q["state"], q["project"]) for q in (dict(urllib.parse.parse_qsl(urllib.parse.urlparse(p).query)) for p in queue.paths[1:]))
        self.assertEqual(asked, [("dismissed", OTHER_PROJECT), ("dismissed", PROJECT), ("snoozed", OTHER_PROJECT), ("snoozed", PROJECT)])
        self.assertEqual(view["snoozed"], [{"id": "upgrade-pdb:s", "severity": "critical", "project": OTHER_PROJECT, "cluster": "seeded-b", "snoozed_until": "2026-11-01T00:00:00Z"}])

    def test_a_full_page_is_named_cut(self):
        full = [row(f"cost:{n}", check="cost-idle-node", state="dismissed") for n in range(brief_queue.PAGE_LIMIT)]
        code, view = self.run_main(FakeQueue([row("upgrade-pdb:a")], {("dismissed", PROJECT): full}))
        self.assertEqual(view["cut"], [{"state": "dismissed", "project": PROJECT}])
        self.assertEqual(view["dismissed"], [], "the page was full of other sources' rows")

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
    def test_the_brief_turn_holds_no_mcp_server_and_reads_the_queue_before_it(self):
        jobs = {job["id"]: job for job in json.loads(ROSTER.read_text())["jobs"]}
        brief = jobs[review.BRIEF_JOB_ID]
        self.assertEqual(brief["script"], Path(brief_queue.__file__).name)
        toolsets = brief["enabled_toolsets"]
        # The sentinel drops every MCP server the profile enables; a server named
        # beside it is kept, and platform_control would bring update_finding.
        self.assertIn("no_mcp", toolsets)
        servers = set()
        for config in (PLATFORM_CONFIG, SHARED_CONFIG):
            servers |= set((yaml.safe_load(config.read_text()) or {}).get("mcp_servers") or {})
        self.assertIn("platform_control", servers)
        self.assertEqual({t.removeprefix("mcp-") for t in toolsets} & servers, set())
        self.assertNotIn("hermes-cli", toolsets, "the composite toolset carries delegate_task and the kanban tools")
        self.assertNotIn("file", toolsets, "write_file and patch are not held to the risk: high command policy")


if __name__ == "__main__":
    unittest.main()
