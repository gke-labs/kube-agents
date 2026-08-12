#!/usr/bin/env python3
"""Tests for pr_conversation.py, the pr-conversation skill's helper.

Two properties carry the weight:

* **`reply` always stamps the marker.** The marker is the entire idempotency
  scheme, and the failure mode of a missing one is not a missing comment — it is
  the same request being answered every ten minutes forever. So the helper
  appends it from `--comment-id` rather than trusting the model to type it.
* **A reply body cannot come from outside the scratch directory.** The body is
  posted publicly, so the path is confined by `realpath` rather than merely
  checked for existence — the same rule `resolver.handle_transition` applies to
  its report file, and for the same reason.

Driven by a fake provider, like `test_github_scan_gate.py`: what is under test
is the helper's contract, not `gh`'s argv, which `test_forge.py` pins.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parents[2] / "scripts"  # agents/platform/scripts
sys.path.insert(0, str(_SHARED))

import forge  # noqa: E402
import pr_triggers  # noqa: E402


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "pr_conversation_under_test", _HERE / "pr_conversation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()

SELF = "kube-agents-bot"


class FakeProvider:
    supports_acknowledge = True

    def __init__(self, prs=None, comments=None, post_error=None):
        self.prs = prs or []
        self.comments = comments or {}
        self.post_error = post_error
        self.posted = []

    def preflight(self):
        pass

    def self_login(self, pr):
        return SELF

    def list_open_prs(self, repo):
        return list(self.prs)

    def list_comments(self, repo, pr):
        return list(self.comments.get(pr.number, []))

    def post_comment(self, repo, pr, body_file):
        if self.post_error:
            raise self.post_error
        with open(body_file, "r", encoding="utf-8") as handle:
            self.posted.append((pr.number, handle.read()))


def make_pr(number=12, head_ref="platform-agent/x", labels=()):
    return forge.PullRequest(
        number=number, head_ref=head_ref, author=f"{SELF}[bot]", labels=labels
    )


def make_comment(node_id, body, author="reviewer", can_write=True):
    return forge.Comment(
        node_id=node_id,
        numeric_id=1,
        author=author,
        body=body,
        can_write=can_write,
        created_at="2026-08-12T10:00:00Z",
    )


class _Harness(unittest.TestCase):
    """Runs the helper against a fake provider and a scratch directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.scratch = os.path.join(self._tmp.name, "scratch")
        os.makedirs(self.scratch)
        patch = mock.patch.object(helper, "SCRATCH_DIR", self.scratch)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def scratch_file(self, name, content):
        path = os.path.join(self.scratch, name)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def run_helper(self, argv, provider, repo="acme/toolkit", repo_error=None):
        target = (
            mock.Mock(side_effect=repo_error)
            if repo_error
            else mock.Mock(return_value=repo)
        )
        buf = StringIO()
        with mock.patch.object(forge, "target_repo", target), \
             mock.patch.object(forge, "provider_for", return_value=provider), \
             redirect_stdout(buf):
            rc = helper.main(argv)
        return rc, buf.getvalue()


class PollTest(_Harness):
    def test_no_repo_configured(self):
        _rc, out = self.run_helper(["poll"], FakeProvider(), repo=None)
        self.assertEqual(json.loads(out)["status"], "NOT_CONFIGURED")

    def test_no_requests(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "looks good")]}
        )
        _rc, out = self.run_helper(["poll"], provider)
        self.assertEqual(json.loads(out)["status"], "NO_REQUESTS")

    def test_a_trigger_is_reported_with_its_context(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "/agent bump to 4")]}
        )
        _rc, out = self.run_helper(["poll"], provider)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "FOUND")
        row = payload["requests"][0]
        self.assertEqual(row["comment_id"], "IC_1")
        self.assertEqual(row["request"], "bump to 4")
        self.assertEqual(row["kind"], "slash")
        self.assertEqual(row["head_ref"], "platform-agent/x")
        self.assertTrue(row["can_write"])

    def test_an_untrusted_request_is_reported_rather_than_hidden(self):
        """The worker is told so it can refuse, not left looking like it missed it."""
        provider = FakeProvider(
            prs=[make_pr()],
            comments={12: [make_comment("IC_1", "/agent x", can_write=False)]},
        )
        _rc, out = self.run_helper(["poll"], provider)
        self.assertFalse(json.loads(out)["requests"][0]["can_write"])

    def test_an_answered_request_is_not_reported(self):
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment("IC_1", "/agent x"),
                    make_comment(
                        "IC_9", pr_triggers.marker("IC_1"), author=f"{SELF}[bot]"
                    ),
                ]
            },
        )
        _rc, out = self.run_helper(["poll"], provider)
        self.assertEqual(json.loads(out)["status"], "NO_REQUESTS")

    def test_pr_filter_narrows_the_scan(self):
        provider = FakeProvider(
            prs=[make_pr(12), make_pr(13)],
            comments={
                12: [make_comment("IC_1", "/agent a")],
                13: [make_comment("IC_2", "/agent b")],
            },
        )
        _rc, out = self.run_helper(["poll", "--pr", "13"], provider)
        rows = json.loads(out)["requests"]
        self.assertEqual([r["comment_id"] for r in rows], ["IC_2"])

    def test_a_pr_the_agent_did_not_author_is_out_of_scope(self):
        provider = FakeProvider(
            prs=[make_pr(head_ref="feat/human")],
            comments={12: [make_comment("IC_1", "/agent x")]},
        )
        _rc, out = self.run_helper(["poll"], provider)
        self.assertEqual(json.loads(out)["status"], "NO_REQUESTS")

    def test_a_forge_fault_reports_its_reason_code(self):
        class Broken(FakeProvider):
            def list_open_prs(self, repo):
                raise forge.ForgeError("REPO_UNREACHABLE", "HTTP 404")

        _rc, out = self.run_helper(["poll"], Broken())
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "REPO_UNREACHABLE")

    def test_an_unparseable_repo_reports_its_reason_code(self):
        _rc, out = self.run_helper(
            ["poll"], FakeProvider(), repo_error=forge.RepoUnparseable("evil.com/a/b")
        )
        self.assertEqual(json.loads(out)["reason"], "GIT_REPO_UNPARSEABLE")


class ReplyTest(_Harness):
    def _reply(self, provider, body="Bumped it to 4.", command="reply"):
        path = self.scratch_file("reply.md", body)
        return self.run_helper(
            [command, "--pr", "12", "--comment-id", "IC_1", "--body-file", path],
            provider,
        )

    def test_the_marker_is_appended_by_the_helper(self):
        """The model cannot forget it, because the model does not write it."""
        provider = FakeProvider(prs=[make_pr()])
        self._reply(provider)
        _number, posted = provider.posted[0]
        self.assertIn("Bumped it to 4.", posted)
        self.assertIn("<!-- agent-answered:IC_1 -->", posted)

    def test_the_posted_marker_reads_back_as_handled(self):
        """Round-trip: what `reply` writes is what the sweep's scan looks for."""
        provider = FakeProvider(prs=[make_pr()])
        self._reply(provider)
        _number, posted = provider.posted[0]
        self.assertEqual(
            pr_triggers.handled_node_ids(
                [make_comment("IC_9", posted, author=f"{SELF}[bot]")], SELF
            ),
            {"IC_1"},
        )

    def test_refuse_uses_the_refusal_marker(self):
        provider = FakeProvider(prs=[make_pr()])
        self._reply(provider, body="Not in scope.", command="refuse")
        self.assertIn("<!-- agent-refused:IC_1 -->", provider.posted[0][1])

    def test_the_success_line_is_machine_readable(self):
        provider = FakeProvider(prs=[make_pr()])
        _rc, out = self._reply(provider)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "POSTED")
        self.assertEqual(payload["comment_id"], "IC_1")

    def test_a_body_outside_scratch_is_rejected(self):
        provider = FakeProvider(prs=[make_pr()])
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("elsewhere")
            outside = handle.name
        self.addCleanup(os.unlink, outside)
        with self.assertRaises(SystemExit):
            self.run_helper(
                ["reply", "--pr", "12", "--comment-id", "IC_1", "--body-file", outside],
                provider,
            )
        self.assertEqual(provider.posted, [])

    def test_a_symlink_out_of_scratch_is_rejected(self):
        """`realpath` before the prefix check, not after."""
        provider = FakeProvider(prs=[make_pr()])
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("elsewhere")
            outside = handle.name
        self.addCleanup(os.unlink, outside)
        link = os.path.join(self.scratch, "link.md")
        os.symlink(outside, link)
        with self.assertRaises(SystemExit):
            self.run_helper(
                ["reply", "--pr", "12", "--comment-id", "IC_1", "--body-file", link],
                provider,
            )
        self.assertEqual(provider.posted, [])

    def test_a_missing_body_is_rejected(self):
        provider = FakeProvider(prs=[make_pr()])
        with self.assertRaises(SystemExit):
            self.run_helper(
                [
                    "reply",
                    "--pr",
                    "12",
                    "--comment-id",
                    "IC_1",
                    "--body-file",
                    os.path.join(self.scratch, "nope.md"),
                ],
                provider,
            )

    def test_an_empty_body_is_rejected(self):
        """An empty comment marks the request answered without answering it."""
        provider = FakeProvider(prs=[make_pr()])
        with self.assertRaises(SystemExit):
            self._reply(provider, body="   \n")
        self.assertEqual(provider.posted, [])

    def test_a_pr_that_is_not_open_is_rejected(self):
        provider = FakeProvider(prs=[make_pr(13)])
        with self.assertRaises(SystemExit):
            self._reply(provider)

    def test_a_failed_post_exits_non_zero(self):
        provider = FakeProvider(
            prs=[make_pr()], post_error=forge.ForgeError("REPO_UNREACHABLE", "403")
        )
        with self.assertRaises(SystemExit) as ctx:
            self._reply(provider)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_the_stamped_copy_does_not_survive_the_run(self):
        provider = FakeProvider(prs=[make_pr()])
        self._reply(provider)
        leftovers = [
            name for name in os.listdir(self.scratch) if name != "reply.md"
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
