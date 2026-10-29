"""The broker files a confirmed kube-agents report once, refuses unconfirmed text, and cannot drift from the form.

The incident these tests encode: a live agent asked to file a report POSTed the
form by hand, missed the confirmation in the response, POSTed again, and filed
the same report twice (gke-labs/kube-agents#1345 and #1346). The verb exists so
that path is deterministic and runs where the sandbox cannot reach (the broker
pod has egress; the shell sandbox has DNS and the broker only); these tests
hold it to that.

Run:
  python3 -m unittest test_feedback_broker -v   (from agents/platform/scripts)
"""

from __future__ import annotations

import http.client
import json
import re
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import feedback_broker as fb

REPO_ROOT = Path(__file__).resolve().parents[3]
CODE_GS = REPO_ROOT / "scripts/feedback_form/Code.gs"

SHORT_LINK_HTML = (
    '<meta http-equiv="refresh" content="0; url='
    "https://docs.google.com/forms/d/e/FAKEFORMID123/viewform\">"
)

FB_DATA = [
    None,
    [
        None,
        [
            [1, "One-line summary", None, 0, [[2090359754, None, 1]]],
            [2, "What kind of feedback is this?", None, 2, [[1895537695, None, 1]]],
            [3, "What happened?", None, 1, [[1875927715, None, 1]]],
            [4, "What did you expect instead?", None, 1, [[1515099816, None, 0]]],
            [5, "Version and environment", None, 1, [[73334924, None, 0]]],
        ],
    ],
]
VIEWFORM_HTML = "<script>var FB_PUBLIC_LOAD_DATA_ = " + json.dumps(FB_DATA) + ";</script>"
CONFIRMATION_HTML = (
    "<html>Thanks. Your report is being filed as a public issue on "
    "github.com/gke-labs/kube-agents/issues and should appear there "
    "within a minute.</html>"
)

PAYLOAD = {
    "title": "credential proxy dies silently",
    "kind": "Bug",
    "happened": "the sidecar crash-loops and nothing surfaces it",
    "expected": "a degraded-mode warning",
    "environment": "chart 0.9, GKE 1.33",
    "user_confirmed": True,
}

ISSUE_URL = "https://github.com/gke-labs/kube-agents/issues/9999"
ISSUE_BODY = (
    "Filed from the form.\n\n**Kind:** Bug\n\n## What happened\n\n"
    + PAYLOAD["happened"]
    + "\n\n## What was expected\n\n"
    + PAYLOAD["expected"]
    + "\n"
)
TRACKER_MATCH = json.dumps(
    [{"title": PAYLOAD["title"], "created_at": "2099-01-01T00:00:00Z", "html_url": ISSUE_URL, "body": ISSUE_BODY}]
)
TRACKER_EMPTY = "[]"
FALLBACK = {"tracker": "https://github.com/gke-labs/kube-agents/issues",
            "form": "https://gke-labs.github.io/kube-agents/feedback"}


class FakeWire:
    """Canned responses per URL, a log of every POST, a realistic tracker.

    Like the real pipeline, the tracker shows the issue only after a POST has
    gone out, which is what lets the pre-POST duplicate probe and the post-POST
    lookup be told apart.
    """

    def __init__(self, form_response_body: str = CONFIRMATION_HTML, tracker_seeded: bool = False):
        self.posts: list[tuple[str, bytes]] = []
        self.form_response_body = form_response_body
        self.tracker_seeded = tracker_seeded

    def tracker_response(self) -> str:
        if self.tracker_seeded or self.posts:
            return TRACKER_MATCH
        return TRACKER_EMPTY

    def __call__(self, url: str, data: bytes | None = None) -> str:
        if data is not None:
            self.posts.append((url, data))
            return self.form_response_body
        if url.startswith(fb.SHORT_LINK):
            return SHORT_LINK_HTML
        if "viewform" in url:
            return VIEWFORM_HTML
        if url.startswith(fb.TRACKER_API):
            return self.tracker_response()
        raise AssertionError(f"unexpected GET {url}")


class PostFailsWire(FakeWire):
    """The POST dies on the wire; its attempt is still logged."""

    def __call__(self, url: str, data: bytes | None = None) -> str:
        if data is not None:
            self.posts.append((url, data))
            raise urllib.error.URLError("connection reset")
        return super().__call__(url, data)


class SubmitTest(unittest.TestCase):
    def setUp(self) -> None:
        # One tracker read per lookup: the poll window is wall-clock time,
        # which a unit test does not spend.
        patch = mock.patch.object(fb, "TRACKER_POLL_SECONDS", 0)
        patch.start()
        self.addCleanup(patch.stop)

    def _submit(self, payload: dict, wire: FakeWire) -> dict:
        return fb.submit(payload, fetch=wire, sleep=lambda _: None)

    def test_submits_once_and_reports_the_issue_url(self) -> None:
        wire = FakeWire()
        out = self._submit(PAYLOAD, wire)
        self.assertEqual("SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"])
        self.assertEqual(1, len(wire.posts), "exactly one POST per payload")
        url, body = wire.posts[0]
        self.assertIn("formResponse", url)
        self.assertIn(b"entry.2090359754", body)

    def test_missed_confirmation_is_settled_by_the_tracker(self) -> None:
        # 1345/1346: the POST landed but the confirmation was not recognized.
        wire = FakeWire(form_response_body="<html>something unrecognized</html>")
        out = self._submit(PAYLOAD, wire)
        self.assertEqual("SUBMITTED", out["status"], "tracker match settles it")
        self.assertEqual(ISSUE_URL, out["issue_url"])

    def test_recent_issue_with_this_title_is_not_filed_again(self) -> None:
        # A second card carrying the same confirmed text; the pre-POST
        # tracker probe is what keeps it from re-filing.
        wire = FakeWire(tracker_seeded=True)
        out = self._submit(PAYLOAD, wire)
        self.assertEqual("ALREADY_SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"])
        self.assertEqual([], wire.posts, "nothing may go out past the probe")

    def test_failed_post_reports_unconfirmed(self) -> None:
        wire = PostFailsWire()
        out = self._submit(PAYLOAD, wire)
        self.assertEqual("UNCONFIRMED", out["status"])
        self.assertIsNone(out["issue_url"])
        self.assertEqual(1, len(wire.posts))

    def test_recheck_only_never_posts(self) -> None:
        # The client's path after a lost answer: the tracker is the arbiter.
        # PostFailsWire's tracker shows the issue once a POST attempt was made,
        # mirroring a POST that landed despite the error the caller saw.
        wire = PostFailsWire()
        self._submit(PAYLOAD, wire)
        out = self._submit(dict(PAYLOAD, recheck_only=True), wire)
        self.assertEqual("ALREADY_SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"])
        self.assertEqual(1, len(wire.posts))

    def test_lost_submission_is_never_reported_as_success(self) -> None:
        # Recheck, and the tracker never shows the issue: UNCONFIRMED (hand it
        # to a human), not a null-URL success, and still no POST.
        wire = FakeWire()
        out = self._submit(dict(PAYLOAD, recheck_only=True), wire)
        self.assertEqual("UNCONFIRMED", out["status"])
        self.assertIsNone(out["issue_url"])
        self.assertIn("human", out["detail"])
        self.assertEqual([], wire.posts)

    def test_unconfirmed_text_is_refused(self) -> None:
        wire = FakeWire()
        with self.assertRaises(fb.Blocked) as ctx:
            self._submit(dict(PAYLOAD, user_confirmed=False), wire)
        self.assertIn("user_confirmed", ctx.exception.reason)
        self.assertEqual([], wire.posts, "nothing goes out unconfirmed")

    def test_missing_required_field_is_refused(self) -> None:
        for field in fb.REQUIRED_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(fb.Blocked):
                    self._submit(dict(PAYLOAD, **{field: "  "}), FakeWire())

    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(fb.Blocked):
            self._submit(dict(PAYLOAD, kind="Complaint"), FakeWire())

    def test_title_at_the_cap_is_accepted_and_one_over_is_refused(self) -> None:
        fb.validate(fb.normalize(dict(PAYLOAD, title="x" * 120)))
        with self.assertRaises(fb.Blocked) as ctx:
            fb.validate(fb.normalize(dict(PAYLOAD, title="x" * 121)))
        self.assertIn("120", ctx.exception.reason)

    def test_values_are_stripped_before_posting_and_matching(self) -> None:
        wire = FakeWire()
        out = self._submit(dict(PAYLOAD, title="  " + PAYLOAD["title"] + "  "), wire)
        self.assertEqual("SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"], "the stripped title matches")

    def test_secret_shaped_text_is_refused_before_any_request(self) -> None:
        wire = FakeWire()
        with self.assertRaises(fb.Blocked):
            self._submit(dict(PAYLOAD, happened="here: ghp_" + "a" * 36 + " broke"), wire)
        self.assertEqual([], wire.posts)

    def test_retired_form_blocks_with_a_pointer(self) -> None:
        class RetiredWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(fb.SHORT_LINK):
                    return "<html>404 not found</html>"
                return super().__call__(url, data)

        with self.assertRaises(fb.Blocked) as ctx:
            self._submit(PAYLOAD, RetiredWire())
        self.assertIn("retired", ctx.exception.reason)

    def test_unreachable_form_blocks_and_sends_nothing(self) -> None:
        class DownWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(fb.TRACKER_API):
                    return TRACKER_EMPTY
                raise urllib.error.URLError("no route to host")

        wire = DownWire()
        with self.assertRaises(fb.Blocked) as ctx:
            self._submit(PAYLOAD, wire)
        self.assertIn("could not reach", ctx.exception.reason)
        self.assertEqual([], wire.posts, "nothing was sent, as BLOCKED promises")

    def test_a_malformed_form_page_blocks_rather_than_raising(self) -> None:
        class OddFormWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if "viewform" in url:
                    return "<script>var FB_PUBLIC_LOAD_DATA_ = [1];</script>"
                return super().__call__(url, data)

        wire = OddFormWire()
        with self.assertRaises(fb.Blocked) as ctx:
            self._submit(PAYLOAD, wire)
        self.assertIn("form page", ctx.exception.reason)
        self.assertEqual([], wire.posts)

    def test_an_unreadable_tracker_before_the_post_blocks(self) -> None:
        # The pre-POST duplicate probe is one of the two guards against
        # #1345/#1346; blind, it must refuse rather than file.
        class RateLimitedWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(fb.TRACKER_API):
                    raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)
                return super().__call__(url, data)

        wire = RateLimitedWire()
        with self.assertRaises(fb.Blocked) as ctx:
            self._submit(PAYLOAD, wire)
        self.assertIn("tracker", ctx.exception.reason)
        self.assertEqual([], wire.posts, "nothing goes out past a blind probe")

    def test_a_confirmed_post_with_no_issue_yet_says_so(self) -> None:
        class SlowPipelineWire(FakeWire):
            def tracker_response(self) -> str:
                return TRACKER_EMPTY

        out = self._submit(PAYLOAD, SlowPipelineWire())
        self.assertEqual(("SUBMITTED", None), (out["status"], out["issue_url"]))
        self.assertIn("recheck", out["detail"])

    def test_an_unreadable_tracker_after_a_confirmed_post_is_still_submitted(self) -> None:
        class TrackerDiesAfterPostWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(fb.TRACKER_API) and self.posts:
                    raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)
                return super().__call__(url, data)

        out = self._submit(PAYLOAD, TrackerDiesAfterPostWire())
        self.assertEqual(("SUBMITTED", None), (out["status"], out["issue_url"]))
        self.assertIn("tracker", out["detail"])

    def test_a_truncated_response_after_the_post_is_unconfirmed_not_blocked(self) -> None:
        class CutBodyWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if data is not None:
                    self.posts.append((url, data))
                    raise http.client.IncompleteRead(b"<html>partial")
                return super().__call__(url, data)

        wire = CutBodyWire()
        out = self._submit(PAYLOAD, wire)
        self.assertEqual("UNCONFIRMED", out["status"], "the POST went out; this is not BLOCKED")
        self.assertEqual(1, len(wire.posts))

    def test_a_truncated_tracker_read_after_the_post_is_still_submitted(self) -> None:
        class CutTrackerWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(fb.TRACKER_API) and self.posts:
                    raise http.client.BadStatusLine("garbage")
                return super().__call__(url, data)

        out = self._submit(PAYLOAD, CutTrackerWire())
        self.assertEqual(("SUBMITTED", None), (out["status"], out["issue_url"]))

    def test_fine_grained_github_and_slack_tokens_are_refused(self) -> None:
        for leaky in ("auth failed with github_pat_11ABCDEFG0123456789abcdefGHIJ in the log",
                      "token xoxb-1234567890-abcdefghijkl was rejected"):
            wire = FakeWire()
            with self.subTest(leaky=leaky), self.assertRaises(fb.Blocked):
                self._submit(dict(PAYLOAD, happened=leaky), wire)
            self.assertEqual([], wire.posts)

    def test_a_different_report_with_the_same_summary_is_filed(self) -> None:
        # The dedupe reads the issue's "What happened" as well as its title: a
        # second person's report under the same one-line summary is not the
        # same report.
        wire = FakeWire(tracker_seeded=True)
        out = self._submit(dict(PAYLOAD, happened="a different thing happened entirely"), wire)
        self.assertEqual("SUBMITTED", out["status"])
        self.assertEqual(1, len(wire.posts))

    def test_null_optional_fields_are_absent_not_the_word_none(self) -> None:
        wire = FakeWire()
        self._submit(dict(PAYLOAD, expected=None, environment=None), wire)
        _, body = wire.posts[0]
        self.assertNotIn(b"None", body)
        with self.assertRaises(fb.Blocked):
            fb.validate(fb.normalize(dict(PAYLOAD, title=None)))

    def test_a_confirmed_recheck_with_no_issue_yet_stays_submitted(self) -> None:
        # The client's marker says the form confirmed; the pipeline is slow.
        # The recheck must not say the report may be lost.
        class SlowPipelineWire(FakeWire):
            def tracker_response(self) -> str:
                return TRACKER_EMPTY

        out = self._submit(dict(PAYLOAD, recheck_only=True, form_confirmed=True), SlowPipelineWire())
        self.assertEqual(("SUBMITTED", None), (out["status"], out["issue_url"]))
        self.assertIn("recheck", out["detail"])

    def test_a_heading_inside_what_happened_does_not_blind_the_dedupe(self) -> None:
        happened = "Opened chat.\n## Steps\n1. typed /feedback"
        body = ("Filed from the form.\n\n**Kind:** Bug\n\n## What happened\n\n" + happened
                + "\n\n## What was expected\n\nonce\n")
        self.assertTrue(fb._body_carries_happened(body, happened))
        self.assertFalse(fb._body_carries_happened(body, "Opened chat."))
        self.assertIsNone(fb._body_carries_happened("no section here", happened))

        class SeededWire(FakeWire):
            def tracker_response(self) -> str:
                return json.dumps([{"title": PAYLOAD["title"], "created_at": "2099-01-01T00:00:00Z",
                                    "html_url": ISSUE_URL, "body": body}])

        wire = SeededWire()
        out = self._submit(dict(PAYLOAD, happened=happened), wire)
        self.assertEqual(("ALREADY_SUBMITTED", []), (out["status"], wire.posts))

    def test_one_of_the_forms_own_headings_inside_what_happened_does_not_blind_the_dedupe(self) -> None:
        happened = "Filed once already:\n## What was expected\nan issue link"
        body = "## What happened\n\n" + happened + "\n\n## What was expected\n\nonce\n"
        self.assertTrue(fb._body_carries_happened(body, happened))
        self.assertFalse(fb._body_carries_happened(body, "Filed once already:"))
        last_section = "## What happened\n\n" + happened + "\n"
        self.assertTrue(fb._body_carries_happened(last_section, happened))

    def test_a_blind_recheck_past_the_grace_stays_submitted(self) -> None:
        class RateLimitedWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(fb.TRACKER_API):
                    raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)
                return super().__call__(url, data)

        now = 1_800_000_000.0
        out = fb.submit(dict(PAYLOAD, recheck_only=True, form_confirmed=True,
                             form_confirmed_at=now - fb.PIPELINE_GRACE_SECONDS - 60),
                        fetch=RateLimitedWire(), now=lambda: now, sleep=lambda _: None)
        self.assertEqual(("SUBMITTED", None), (out["status"], out["issue_url"]))
        self.assertIn("tracker", out["detail"])

    def test_a_refused_kind_is_not_echoed(self) -> None:
        token = "ghp_" + "a" * 36
        with self.assertRaises(fb.Blocked) as ctx:
            fb.validate(fb.normalize(dict(PAYLOAD, kind=token)))
        self.assertNotIn(token, ctx.exception.reason)
        self.assertIn("Bug", ctx.exception.reason)

    def test_the_tracker_read_covers_the_whole_day(self) -> None:
        seen: list[str] = []

        class RecordingWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(fb.TRACKER_API):
                    seen.append(url)
                return super().__call__(url, data)

        self._submit(PAYLOAD, RecordingWire())
        self.assertTrue(seen)
        self.assertIn("since=", seen[0])
        self.assertIn(f"per_page={fb.TRACKER_PAGE_SIZE}", seen[0])
        self.assertGreaterEqual(fb.TRACKER_PAGE_SIZE, 100)

    def test_the_installs_own_provider_and_oauth_tokens_are_refused(self) -> None:
        for leaky in ("auth failed for sk-ant-api03-" + "Ab1" * 12 + " at litellm",
                      "the relay logged ya29.a0AfB_" + "x" * 40 + " and then 401",
                      "key sk-" + "a" * 24 + " rejected", "header bearer " + "q1w2e3r4" * 4):
            wire = FakeWire()
            with self.subTest(leaky=leaky[:30]), self.assertRaises(fb.Blocked):
                self._submit(dict(PAYLOAD, happened=leaky), wire)
            self.assertEqual([], wire.posts)

    def test_a_body_the_tracker_cannot_hold_is_refused_before_the_post(self) -> None:
        wire = FakeWire()
        with self.assertRaises(fb.Blocked) as ctx:
            self._submit(dict(PAYLOAD, happened="x" * (fb.BODY_MAX_CHARS + 1)), wire)
        self.assertIn(str(fb.BODY_MAX_CHARS), ctx.exception.reason)
        self.assertEqual([], wire.posts)
        fb.validate(fb.normalize(dict(PAYLOAD, happened="x" * (fb.BODY_MAX_CHARS - len(PAYLOAD["expected"]) - len(PAYLOAD["environment"])))))

    def test_a_confirmed_recheck_past_the_pipelines_grace_is_unconfirmed(self) -> None:
        class SlowPipelineWire(FakeWire):
            def tracker_response(self) -> str:
                return TRACKER_EMPTY

        now = 1_800_000_000.0
        fresh = fb.submit(dict(PAYLOAD, recheck_only=True, form_confirmed=True,
                               form_confirmed_at=now - fb.PIPELINE_GRACE_SECONDS + 60),
                          fetch=SlowPipelineWire(), now=lambda: now, sleep=lambda _: None)
        self.assertEqual("SUBMITTED", fresh["status"])
        stale = fb.submit(dict(PAYLOAD, recheck_only=True, form_confirmed=True,
                               form_confirmed_at=now - fb.PIPELINE_GRACE_SECONDS - 60),
                          fetch=SlowPipelineWire(), now=lambda: now, sleep=lambda _: None)
        self.assertEqual("UNCONFIRMED", stale["status"])
        self.assertIn("pipeline", stale["detail"])

    def test_every_answer_carries_the_fallback_links(self) -> None:
        answers = [
            self._submit(PAYLOAD, FakeWire()),
            self._submit(PAYLOAD, FakeWire(tracker_seeded=True)),
            self._submit(PAYLOAD, PostFailsWire()),
            fb.blocked_answer("x"),
        ]
        for answer in answers:
            self.assertEqual(FALLBACK, answer["fallback"], answer["status"])
        self.assertEqual("BLOCKED", answers[-1]["status"])
        self.assertEqual("x", answers[-1]["reason"])


class FormContractTest(unittest.TestCase):
    """The broker's constants stay tied to the form's own builder script."""

    def test_question_labels_match_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        for label in fb.QUESTION_LABELS.values():
            self.assertIn(f"title: '{label}'", code_gs,
                          f"Code.gs no longer builds a question titled {label!r}")

    def test_kind_choices_match_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        match = re.search(r"choices: \[(.*?)\]", code_gs)
        self.assertIsNotNone(match)
        assert match
        self.assertEqual(list(fb.KIND_CHOICES), re.findall(r"'([^']+)'", match.group(1)))

    def test_confirmation_substring_matches_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        # A second run of blanks follows a string only after a "+", so the blanks
        # between two strings are read one way rather than once per split.
        match = re.search(r"CONFIRMATION_MESSAGE =\s*((?:'[^']*'\s*(?:\+\s*)?)+);", code_gs)
        self.assertIsNotNone(match, "no CONFIRMATION_MESSAGE in Code.gs")
        assert match
        message = "".join(re.findall(r"'([^']*)'", match.group(1)))
        self.assertIn(fb.CONFIRMATION_SUBSTRING, message)

    def test_tracker_repo_and_label_match_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        self.assertIn(f"const REPO = '{fb.TRACKER_REPO}'", code_gs)
        self.assertIn(f"const LABEL = '{fb.FEEDBACK_LABEL}'", code_gs)

    def test_title_cap_matches_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        self.assertIn(f"const TITLE_MAX_CHARS = {fb.TITLE_MAX_CHARS};", code_gs)


if __name__ == "__main__":
    unittest.main()
