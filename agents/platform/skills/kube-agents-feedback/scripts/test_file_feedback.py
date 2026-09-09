"""The feedback-filing script files once, refuses unconfirmed text, and cannot drift from the form.

The incident these tests encode: a live agent asked to file a report POSTed the
form by hand, missed the confirmation in the response, POSTed again, and filed
the same report twice (gke-labs/kube-agents#1345 and #1346). The script exists
so that path is deterministic; these tests hold it to that.

Run:
  python3 -m unittest discover -s agents/platform/skills/kube-agents-feedback/scripts \
      -p 'test_file_feedback.py' -v
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import file_feedback

REPO_ROOT = Path(__file__).resolve().parents[5]
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
VIEWFORM_HTML = (
    "<script>var FB_PUBLIC_LOAD_DATA_ = " + json.dumps(FB_DATA) + ";</script>"
)
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
TRACKER_MATCH = json.dumps(
    [
        {
            "title": PAYLOAD["title"],
            "created_at": "2099-01-01T00:00:00Z",
            "html_url": ISSUE_URL,
        }
    ]
)
TRACKER_EMPTY = "[]"


class FakeWire:
    """Canned responses per URL, a log of every POST, a realistic tracker.

    Like the real pipeline, the tracker shows the issue only after a POST has
    gone out — which is exactly what lets the pre-POST duplicate probe and the
    post-POST lookup be told apart in these tests.
    """

    def __init__(self, form_response_body: str = CONFIRMATION_HTML,
                 tracker_seeded: bool = False):
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
        if url.startswith(file_feedback.SHORT_LINK):
            return SHORT_LINK_HTML
        if "viewform" in url:
            return VIEWFORM_HTML
        if url.startswith(file_feedback.TRACKER_API):
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        for patch in (
            mock.patch.object(file_feedback.time, "sleep", lambda _: None),
            # One tracker read per lookup: the poll window is wall-clock time,
            # which a unit test does not spend.
            mock.patch.object(file_feedback, "TRACKER_POLL_SECONDS", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _payload_file(self, payload: dict) -> Path:
        path = self.state_dir / "payload.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _submit(self, payload: dict, wire: FakeWire):
        with mock.patch.object(file_feedback, "_fetch", wire):
            with mock.patch("sys.stdout") as stdout:
                code = file_feedback.submit(self._payload_file(payload), self.state_dir)
        printed = "".join(
            call.args[0] for call in stdout.write.call_args_list if call.args[0].strip()
        )
        return code, json.loads(printed)

    def test_submits_once_and_reports_the_issue_url(self) -> None:
        wire = FakeWire()
        code, out = self._submit(PAYLOAD, wire)
        self.assertEqual(0, code)
        self.assertEqual("SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"])
        self.assertEqual(1, len(wire.posts), "exactly one POST per payload")
        url, body = wire.posts[0]
        self.assertIn("formResponse", url)
        self.assertIn(b"entry.2090359754", body)

    def test_second_submit_of_the_same_text_does_not_post(self) -> None:
        wire = FakeWire()
        self._submit(PAYLOAD, wire)
        code, out = self._submit(PAYLOAD, wire)
        self.assertEqual(0, code)
        self.assertEqual("ALREADY_SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"])
        self.assertEqual(1, len(wire.posts), "the duplicate-filing bug, exactly")

    def test_missed_confirmation_does_not_repost(self) -> None:
        # 1345/1346: the POST landed but the confirmation was not recognized.
        # The rerun must consult the tracker, never the form.
        wire = FakeWire(form_response_body="<html>something unrecognized</html>")
        code, out = self._submit(PAYLOAD, wire)
        self.assertEqual(0, code)
        self.assertEqual("SUBMITTED", out["status"], "tracker match settles it")
        code, out = self._submit(PAYLOAD, wire)
        self.assertEqual("ALREADY_SUBMITTED", out["status"])
        self.assertEqual(1, len(wire.posts))

    def test_recent_issue_with_this_title_blocks_a_fresh_workspace_post(self) -> None:
        # A second card carrying the same confirmed text gets a fresh state
        # dir; the pre-POST tracker probe is what keeps it from re-filing.
        wire = FakeWire(tracker_seeded=True)
        code, out = self._submit(PAYLOAD, wire)
        self.assertEqual(0, code)
        self.assertEqual("ALREADY_SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"])
        self.assertEqual([], wire.posts, "nothing may go out past the probe")

    def test_failed_post_reports_unconfirmed_and_never_reposts(self) -> None:
        wire = PostFailsWire()
        code, out = self._submit(PAYLOAD, wire)
        self.assertEqual(0, code)
        self.assertEqual("UNCONFIRMED", out["status"])
        self.assertIsNone(out["issue_url"])
        self.assertEqual(1, len(wire.posts))
        # The rerun consults the tracker only. PostFailsWire's tracker shows
        # the issue once a POST attempt was made, mirroring a POST that in
        # fact landed despite the error the client saw.
        code, out = self._submit(PAYLOAD, wire)
        self.assertEqual("ALREADY_SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"])
        self.assertEqual(1, len(wire.posts))

    def test_lost_submission_is_never_reported_as_success(self) -> None:
        # Marker exists, no confirmation, and the tracker never shows the
        # issue: that must surface as UNCONFIRMED (hand it to a human), not as
        # a null-URL "ALREADY_SUBMITTED" success.
        class NothingLandsWire(PostFailsWire):
            def tracker_response(self) -> str:
                return TRACKER_EMPTY

        wire = NothingLandsWire()
        self._submit(PAYLOAD, wire)
        code, out = self._submit(PAYLOAD, wire)
        self.assertEqual(0, code)
        self.assertEqual("UNCONFIRMED", out["status"])
        self.assertIn("human", out["detail"])
        self.assertEqual(1, len(wire.posts))

    def test_unconfirmed_text_is_refused(self) -> None:
        wire = FakeWire()
        payload = dict(PAYLOAD, user_confirmed=False)
        with self.assertRaises(SystemExit):
            self._submit(payload, wire)
        self.assertEqual([], wire.posts, "nothing goes out unconfirmed")

    def test_missing_required_field_is_refused(self) -> None:
        for field in file_feedback.REQUIRED_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(SystemExit):
                    self._submit(dict(PAYLOAD, **{field: "  "}), FakeWire())

    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self._submit(dict(PAYLOAD, kind="Complaint"), FakeWire())

    def test_overlong_title_is_refused(self) -> None:
        # The pipeline truncates titles at TITLE_MAX_CHARS, which would leave
        # the filed issue unfindable by exact title; refuse it up front.
        long_title = "x" * (file_feedback.TITLE_MAX_CHARS + 1)
        with self.assertRaises(SystemExit):
            self._submit(dict(PAYLOAD, title=long_title), FakeWire())

    def test_values_are_stripped_before_posting_and_matching(self) -> None:
        wire = FakeWire()
        padded = dict(PAYLOAD, title="  " + PAYLOAD["title"] + "  ")
        code, out = self._submit(padded, wire)
        self.assertEqual("SUBMITTED", out["status"])
        self.assertEqual(ISSUE_URL, out["issue_url"], "the stripped title matches")

    def test_secret_shaped_text_is_refused_before_any_request(self) -> None:
        wire = FakeWire()
        leaky = dict(PAYLOAD, happened="here: ghp_" + "a" * 36 + " broke")
        with self.assertRaises(SystemExit):
            self._submit(leaky, wire)
        self.assertEqual([], wire.posts)

    def test_retired_form_blocks_with_a_pointer(self) -> None:
        class RetiredWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(file_feedback.SHORT_LINK):
                    return "<html>404 not found</html>"
                return super().__call__(url, data)

        with self.assertRaises(SystemExit):
            self._submit(PAYLOAD, RetiredWire())

    def test_unreachable_form_blocks_with_a_status_not_a_traceback(self) -> None:
        class DownWire(FakeWire):
            def __call__(self, url: str, data: bytes | None = None) -> str:
                if url.startswith(file_feedback.TRACKER_API):
                    return TRACKER_EMPTY
                raise urllib.error.URLError("no route to host")

        wire = DownWire()
        with self.assertRaises(SystemExit):
            self._submit(PAYLOAD, wire)
        self.assertEqual([], wire.posts, "nothing was sent, as BLOCKED promises")


class FormContractTest(unittest.TestCase):
    """The script's constants stay tied to the form's own builder script."""

    def test_question_labels_match_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        for label in file_feedback.QUESTION_LABELS.values():
            self.assertIn(
                f"title: '{label}'",
                code_gs,
                f"Code.gs no longer builds a question titled {label!r}; "
                "QUESTION_LABELS must follow it",
            )

    def test_kind_choices_match_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        match = re.search(r"choices: \[(.*?)\]", code_gs)
        self.assertIsNotNone(match)
        assert match
        choices = re.findall(r"'([^']+)'", match.group(1))
        self.assertEqual(list(file_feedback.KIND_CHOICES), choices)

    def test_confirmation_substring_matches_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        match = re.search(
            r"CONFIRMATION_MESSAGE =\s*((?:'[^']*'\s*\+?\s*)+);", code_gs
        )
        self.assertIsNotNone(match, "no CONFIRMATION_MESSAGE in Code.gs")
        assert match
        message = "".join(re.findall(r"'([^']*)'", match.group(1)))
        self.assertIn(
            file_feedback.CONFIRMATION_SUBSTRING,
            message,
            "the script would treat every real submission as unconfirmed",
        )

    def test_tracker_repo_and_label_match_the_form_builder(self) -> None:
        # A label or repo rename in Code.gs would make find_issue's query
        # never match, so every submission would report a null issue_url.
        code_gs = CODE_GS.read_text(encoding="utf-8")
        self.assertIn(f"const REPO = '{file_feedback.TRACKER_REPO}'", code_gs)
        self.assertIn(f"const LABEL = '{file_feedback.FEEDBACK_LABEL}'", code_gs)

    def test_title_cap_matches_the_form_builder(self) -> None:
        code_gs = CODE_GS.read_text(encoding="utf-8")
        self.assertIn(
            f"const TITLE_MAX_CHARS = {file_feedback.TITLE_MAX_CHARS};",
            code_gs,
            "the cap validate() enforces must be the one the pipeline applies",
        )


if __name__ == "__main__":
    unittest.main()
