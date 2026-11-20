"""tag_coverage.py: the tagged share of request lines and prompt tokens, by agent, source and model group."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import schema as F
import tag_coverage

RUN = "devops-bench-0123456789ab"


def line(tokens, run=None, agent=None, group="model-default", **extra):
    entry = {F.EVENT: F.REQUEST_EVENT, F.PROMPT_TOKENS: tokens, F.LITELLM_MODEL_GROUP: group,
             F.EVAL_RUN: run, F.EVAL_AGENT: agent}
    entry.update(extra)
    return entry


LINES = [
    line(1000, f"{RUN}/case-a/1", "default;session"),
    line(3000, f"{RUN}/case-a/1", "platform;card", group="model-strong"),
    line(500, None, "platform;none"),
    line(500, None, None),
    # A lost line: no tokens, still a request.
    {F.EVENT: F.REQUEST_EVENT, F.ERROR: F.ERROR_ANALYSIS_DROPPED, F.EVAL_RUN: f"{RUN}/case-a/2", F.EVAL_AGENT: "cluster-x;card"},
]


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.report = tag_coverage.summarize(LINES)

    def test_overall_counts_requests_and_tokens(self):
        overall = self.report["overall"]
        self.assertEqual((overall["requests"], overall["tagged_requests"]), (5, 3))
        self.assertEqual((overall["prompt_tokens"], overall["tagged_prompt_tokens"]), (5000, 4000))
        self.assertEqual((overall["tagged_request_pct"], overall["tagged_token_pct"]), (60.0, 80.0))

    def test_groups(self):
        self.assertEqual(self.report["by_agent"]["platform"]["tagged_requests"], 1)
        self.assertEqual(self.report["by_agent"]["platform"]["requests"], 2)
        self.assertEqual(self.report["by_agent"][tag_coverage.NO_AGENT]["tagged_requests"], 0)
        self.assertEqual(self.report["by_source"]["card"]["requests"], 2)
        self.assertEqual(self.report["by_model_group"]["model-strong"]["tagged_token_pct"], 100.0)

    def test_untagged_lines_name_their_agent(self):
        self.assertEqual(self.report["untagged_by_agent"], {
            "platform;none": {"requests": 1, "prompt_tokens": 500},
            tag_coverage.NO_AGENT: {"requests": 1, "prompt_tokens": 500},
        })

    def test_runs_list_case_and_repetition(self):
        self.assertEqual(self.report["runs"], {RUN: {
            "case-a/1": {"requests": 2, "prompt_tokens": 4000},
            "case-a/2": {"requests": 1, "prompt_tokens": 0},
        }})

    def test_no_lines_is_zero_not_a_division_error(self):
        self.assertEqual(tag_coverage.summarize([])["overall"]["tagged_request_pct"], 0.0)


class MainTest(unittest.TestCase):
    def test_reads_only_request_lines(self):
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp, True)
        path = os.path.join(temp, "proxy.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({F.EVENT: F.START_EVENT}) + "\n")
            handle.write("not json\n")
            for entry in LINES:
                handle.write(json.dumps(entry) + "\n")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(tag_coverage.main([path, "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["overall"]["requests"], 5)
        out = io.StringIO()
        with redirect_stdout(out):
            tag_coverage.main([path])
        self.assertIn("overall: 3/5 requests (60.0%)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
