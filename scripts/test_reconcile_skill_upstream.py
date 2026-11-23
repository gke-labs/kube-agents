#!/usr/bin/env python3
"""Unit tests for the reconcile script of the skill upstream tracking issues.

Run: cd scripts && python3 -m unittest test_reconcile_skill_upstream

Nothing reaches the network: each test gives the client an opener that answers from a
small model of GitHub in memory. The model keeps its state between calls, so a test can
run the script two times and read what the second run sends.
"""

import contextlib
import datetime
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import reconcile_skill_upstream as reconciler
import skill_overlay as overlay

REPO = "o/r"
NOW = datetime.datetime(2026, 12, 1, 12, 0, tzinfo=datetime.timezone.utc)
OLD = "2026-10-01T00:00:00Z"
NEW = "2026-11-25T00:00:00Z"
OVERLAY_PARTS = ("agents", "platform", "skill-overlays")
PATCH_TEXT = """\
Subject: a change

Why: a reason
Local-Issue: none
Eval-Case: none (a reason)
Retire-When: {retire}

diff --git a/SKILL.md b/SKILL.md
--- a/SKILL.md
+++ b/SKILL.md
@@ -1 +1 @@
-old
+new
"""
OWNERSHIP = """\
| Area | Key paths | Primary | Backup | Notes |
| ---- | --------- | ------- | ------ | ----- |
| Agent blueprints | `agents/` | someone | | |
| Upstream skill intake (google/skills) | `third_party/google-skills/` | area-owner | backup | notes |
"""


def path_of(skill, name):
    return "/".join((*OVERLAY_PARTS, skill, name))


def commit(sha, date, login="author-a"):
    return {"sha": sha * 40, "commit": {"committer": {"date": date}}, "author": {"login": login} if login else None}


class FakeGitHub:
    """A model of the endpoints that the script calls. `calls` holds each (method, path)."""

    def __init__(self):
        self.issues = []
        self.comments = {}
        self.commits = {}
        self.labels = set()
        self.refused_assignees = set()
        # {(method, path): status}: GitHub refuses the request each time and does nothing.
        self.refuse = {}
        # [(method, path, error)]: GitHub does the request one time, and the client gets the error.
        self.lose_answer = []
        # [(method, path, error)]: the client gets the error one time, and GitHub does nothing.
        self.fail_first = []
        self.calls = []

    def add_issue(self, path, state="open", number=None, body=None, labels=(reconciler.LABEL,)):
        number = number or len(self.issues) + 1
        body = reconciler.MARKER.format(path=path) if body is None else body
        self.issues.append({
            "number": number,
            "state": state,
            "body": f"text\n\n{body}\n",
            "labels": [{"name": label} for label in labels],
            "assignees": [],
        })
        return number

    def issue(self, number):
        return next(issue for issue in self.issues if issue["number"] == number)

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def _fail(self, request, code):
        raise urllib.error.HTTPError(request.full_url, code, "refused", {}, None)

    def _page(self, items, query):
        size, page = int(query["per_page"][0]), int(query["page"][0])
        return items[(page - 1) * size: page * size]

    def opener(self, request):
        method = request.get_method()
        url = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(url.query)
        payload = json.loads(request.data) if request.data else None
        self.calls.append((method, url.path))
        if (method, url.path) in self.refuse:
            self._fail(request, self.refuse[(method, url.path)])
        for index, (failed_method, failed_path, error) in enumerate(self.fail_first):
            if (failed_method, failed_path) == (method, url.path):
                del self.fail_first[index]
                raise error
        answer = self._do(request, method, url, query, payload)
        for index, (lost_method, lost_path, error) in enumerate(self.lose_answer):
            if (lost_method, lost_path) == (method, url.path):
                del self.lose_answer[index]
                raise error
        return answer

    def _do(self, request, method, url, query, payload):
        prefix = f"/repos/{REPO}"
        if method == "GET" and url.path == f"{prefix}/issues":
            assert query["state"] == ["all"], query
            wanted = query["labels"][0]
            found = [i for i in self.issues if wanted in [label["name"] for label in i["labels"]]]
            return self._answer(self._page(found, query))
        if method == "GET" and url.path == f"{prefix}/commits":
            assert query["sha"] == ["main"], query
            return self._answer(self._page(self.commits.get(query["path"][0], []), query))
        if method == "POST" and url.path == f"{prefix}/labels":
            if payload["name"] in self.labels:
                self._fail(request, 422)
            self.labels.add(payload["name"])
            return self._answer({})
        if method == "POST" and url.path == f"{prefix}/issues":
            assignees = payload.get("assignees", [])
            if self.refused_assignees.intersection(assignees):
                self._fail(request, 422)
            number = len(self.issues) + 1
            self.issues.append({
                "number": number,
                "state": "open",
                "title": payload["title"],
                "body": payload["body"],
                "labels": [{"name": label} for label in payload["labels"]],
                "assignees": [{"login": login} for login in assignees],
            })
            return self._answer({"number": number})
        match = re.fullmatch(rf"{prefix}/issues/(\d+)/comments", url.path)
        if method == "POST" and match:
            self.comments.setdefault(int(match.group(1)), []).append(payload["body"])
            return self._answer({})
        match = re.fullmatch(rf"{prefix}/issues/(\d+)", url.path)
        if method == "PATCH" and match:
            self.issue(int(match.group(1))).update(payload)
            return self._answer({})
        raise AssertionError(f"the script sent a request that the model does not know: {method} {request.full_url}")

    @staticmethod
    def _answer(value):
        raw = json.dumps(value).encode()
        return mock.MagicMock(
            __enter__=mock.Mock(return_value=mock.Mock(read=lambda: raw)),
            __exit__=mock.Mock(return_value=False),
        )


class ReconcileCase(unittest.TestCase):
    """A throwaway repository root with an overlay folder, and a model of GitHub."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.overlay_root = self.root.joinpath(*OVERLAY_PARTS)
        self.overlay_root.mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "ownership.md").write_text(OWNERSHIP)
        self.enterContext(mock.patch.object(overlay, "REPO_ROOT", self.root))
        self.enterContext(mock.patch.object(overlay, "OVERLAY_ROOT", self.overlay_root))
        self.github = FakeGitHub()

    def patch(self, skill, name, retire, added=OLD, login="author-a"):
        folder = self.overlay_root / skill
        folder.mkdir(exist_ok=True)
        (folder / overlay.LOCK_NAME).write_text("commit: 0\n")
        (folder / name).write_text(PATCH_TEXT.format(retire=retire))
        path = path_of(skill, name)
        if added:
            self.github.commits[path] = [commit("a", added, login)]
        return path

    def run_script(self, dry_run=False):
        self.pauses = []
        api = reconciler.GitHubAPI(REPO, "t", dry_run=dry_run, opener=self.github.opener, sleep=self.pauses.append)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            code = reconciler.reconcile(api, REPO, NOW, dry_run)
        self.err = err.getvalue()
        return code, out.getvalue()


class TableTest(ReconcileCase):
    """Each row of the reconcile table, and the rows where nothing happens."""

    def test_a_general_patch_past_the_soak_window_gets_an_issue(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"open     {path}", out)
        (issue,) = self.github.issues
        self.assertEqual(issue["title"], "skills: report gke-a/0001-x.patch to google/skills")
        self.assertEqual(issue["body"].rstrip().splitlines()[-1], f"<!-- skill-upstream-tracking path={path} -->")
        self.assertIn(f"[`{path}`](https://github.com/o/r/blob/main/{path})", issue["body"])
        self.assertIn(f"- Added by: https://github.com/o/r/commit/{'a' * 40} on 2026-10-01", issue["body"])
        self.assertIn("`make skills-sync SKILL=gke-a`", issue["body"])
        self.assertIn(
            "[`skill-upstream-tracking.yml`](https://github.com/o/r/blob/main/.github/workflows/skill-upstream-tracking.yml)",
            issue["body"],
        )
        self.assertEqual([label["name"] for label in issue["labels"]], ["skills:upstream", "needs-human", "priority:p3"])
        self.assertEqual(issue["assignees"], [{"login": "author-a"}])
        self.assertIn("skills:upstream", self.github.labels)

    def test_the_possessive_first_word_is_general_too(self):
        self.patch("gke-a", "0001-x.patch", "upstream's file has LF line endings")
        self.run_script()
        self.assertEqual(len(self.github.issues), 1)

    def test_a_general_patch_in_the_soak_window_waits(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix", added=NEW)
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"wait     {path} (the soak window ends on 2026-12-09)", out)
        self.assertEqual(self.github.writes(), [])

    def test_the_soak_window_starts_at_the_oldest_commit_of_the_file(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        # Newest first, as the API lists them: a header edit last week, the file added in October.
        self.github.commits[path] = [commit("b", NEW, "editor"), commit("a", OLD, "author-a")]
        self.run_script()
        (issue,) = self.github.issues
        self.assertIn(f"commit/{'a' * 40} on 2026-10-01", issue["body"])
        self.assertEqual(issue["assignees"], [{"login": "author-a"}])

    def test_a_general_patch_with_a_closed_issue_gets_it_opened_again(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        number = self.github.add_issue(path, state="closed")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"reopen   {path} (#{number}", out)
        self.assertEqual(self.github.issue(number)["state"], "open")
        (comment,) = self.github.comments[number]
        self.assertIn("opens this issue again", comment)
        self.assertEqual(len(self.github.issues), 1)

    def test_a_specific_patch_with_an_open_issue_gets_it_closed_as_not_planned(self):
        for retire in ("never; the rule is ours", "local the wrapper script is gone"):
            with self.subTest(retire=retire):
                self.github = FakeGitHub()
                path = self.patch("gke-a", "0001-x.patch", retire)
                number = self.github.add_issue(path)
                code, out = self.run_script()
                self.assertEqual(code, 0)
                self.assertIn(f"close    {path} (#{number}, the patch is specific", out)
                issue = self.github.issue(number)
                self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "not_planned"))
                (comment,) = self.github.comments[number]
                self.assertIn(f"`{retire.split()[0]}`", comment)

    def test_a_patch_that_is_gone_gets_its_open_issue_closed_as_completed(self):
        self.patch("gke-a", "0001-stays.patch", "never; ours")
        gone = path_of("gke-a", "0002-gone.patch")
        self.github.commits[gone] = [commit("d", NEW), commit("a", OLD)]
        number = self.github.add_issue(gone)
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"close    {gone} (#{number}, the patch file is gone)", out)
        issue = self.github.issue(number)
        self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "completed"))
        (comment,) = self.github.comments[number]
        self.assertIn(f"https://github.com/o/r/commit/{'d' * 40}", comment)
        # main does not record why a file is gone, so the comment does not say that upstream has the fix.
        self.assertNotIn("upstream", comment.lower())

    def test_a_patch_that_is_gone_closes_also_when_no_commit_is_read(self):
        self.patch("gke-a", "0001-stays.patch", "never; ours")
        number = self.github.add_issue(path_of("gke-a", "0002-gone.patch"))
        self.run_script()
        self.assertEqual(self.github.issue(number)["state"], "closed")
        self.assertIn("could not read the commit", self.github.comments[number][0])

    def test_the_rows_with_no_action_write_nothing(self):
        tracked = self.patch("gke-a", "0001-tracked.patch", "upstream carries the fix")
        self.github.add_issue(tracked)
        specific = self.patch("gke-a", "0002-specific.patch", "never; ours")
        self.github.add_issue(specific, state="closed")
        self.patch("gke-a", "0003-specific-no-issue.patch", "local a change here")
        self.github.add_issue(path_of("gke-a", "0004-gone.patch"), state="closed")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"tracked  {tracked} (#1 is open)", out)
        self.assertEqual(self.github.writes(), [])

    def test_a_comment_goes_before_the_state_change(self):
        for action, retire, state in (
            ("close a specific patch", "never; ours", "open"),
            ("open again", "upstream carries the fix", "closed"),
            ("close a patch that is gone", None, "open"),
        ):
            with self.subTest(action=action):
                self.github = FakeGitHub()
                self.patch("gke-a", "0009-other.patch", "never; ours")
                path = self.patch("gke-a", "0001-x.patch", retire) if retire else path_of("gke-a", "0002-gone.patch")
                number = self.github.add_issue(path, state=state)
                self.run_script()
                self.assertEqual(
                    self.github.writes(),
                    [("POST", f"/repos/o/r/issues/{number}/comments"), ("PATCH", f"/repos/o/r/issues/{number}")],
                )

    def test_a_patch_opens_on_the_day_that_its_soak_window_ends(self):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix", added="2026-11-17T12:00:00Z")
        self.run_script()
        self.assertEqual(len(self.github.issues), 1)

    def test_a_rename_by_hand_closes_the_old_issue_and_the_new_path_waits(self):
        old = path_of("gke-a", "0001-old-name.patch")
        self.github.commits[old] = [commit("b", NEW, "renamer"), commit("a", OLD, "author-a")]
        number = self.github.add_issue(old)
        new = self.patch("gke-a", "0001-new-name.patch", "upstream carries the fix", added=NEW, login="renamer")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.github.issue(number)["state"], "closed")
        self.assertIn(f"wait     {new} (the soak window ends on 2026-12-09)", out)
        self.assertEqual(len(self.github.issues), 1)

    def test_a_patch_in_a_folder_with_no_lock_file_is_not_gone(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        number = self.github.add_issue(path)
        (self.overlay_root / "gke-a" / overlay.LOCK_NAME).unlink()
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"tracked  {path} (#{number} is open)", out)
        self.assertEqual(self.github.writes(), [])


class SafetyTest(ReconcileCase):
    def test_a_second_run_writes_nothing(self):
        self.patch("gke-a", "0001-open.patch", "upstream carries the fix")
        closes = self.patch("gke-a", "0002-close.patch", "never; ours")
        self.github.add_issue(closes)
        reopens = self.patch("gke-b", "0001-reopen.patch", "upstream carries the fix")
        self.github.add_issue(reopens, state="closed")
        self.github.add_issue(path_of("gke-b", "0002-gone.patch"))
        self.run_script()
        self.assertEqual(len(self.github.writes()), 8)  # the label, one issue, and three times a comment and a state
        self.github.calls.clear()
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.github.writes(), [])
        self.assertIn("Done: 0 actions.", out)

    def test_a_run_waits_between_two_write_actions(self):
        for index in range(3):
            self.patch("gke-a", f"000{index}-x.patch", "upstream carries the fix")
        self.run_script()
        self.assertEqual(len(self.github.issues), 3)
        self.assertEqual(self.pauses, [reconciler.WRITE_PAUSE_SECONDS] * 2)

    def test_a_dry_run_prints_the_actions_and_sends_only_reads(self):
        opens = self.patch("gke-a", "0001-open.patch", "upstream carries the fix")
        closes = self.patch("gke-a", "0002-close.patch", "never; ours")
        self.github.add_issue(closes)
        code, out = self.run_script(dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn(f"open     {opens}", out)
        self.assertIn(f"close    {closes}", out)
        self.assertIn("Dry run: 2 actions, nothing written.", out)
        self.assertEqual({method for method, _ in self.github.calls}, {"GET"})
        self.assertEqual(len(self.github.issues), 1)

    def test_the_client_of_a_dry_run_refuses_each_request_that_is_not_a_get(self):
        api = reconciler.GitHubAPI(REPO, "t", dry_run=True, opener=self.github.opener)
        for send in (
            lambda: api.ensure_label(),
            lambda: api.create_issue("t", "b", "someone"),
            lambda: api.comment(1, "b"),
            lambda: api.set_state(1, "closed", "completed"),
            lambda: api.delete("/repos/o/r/issues/1/labels/x"),
        ):
            with self.assertRaises(reconciler.DryRunWrite):
                send()
        self.assertEqual(self.github.calls, [])

    def test_a_run_stops_and_writes_nothing_above_the_limit_of_new_issues(self):
        for index in range(3):
            self.patch("gke-a", f"000{index}-x.patch", "upstream carries the fix")
        with mock.patch.object(reconciler, "MAX_ISSUES_OPENED", 2):
            code, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("above the limit of 2", self.err)
        self.assertEqual(self.github.writes(), [])
        with mock.patch.object(reconciler, "MAX_ISSUES_OPENED", 2):
            code, _ = self.run_script(dry_run=True)
        self.assertEqual(code, 1)
        with mock.patch.object(reconciler, "MAX_ISSUES_OPENED", 3):
            code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.github.issues), 3)

    def test_the_limit_holds_the_general_patches_of_main(self):
        self.assertGreaterEqual(reconciler.MAX_ISSUES_OPENED, 36)

    def test_a_checkout_with_no_patch_closes_nothing(self):
        number = self.github.add_issue(path_of("gke-a", "0001-x.patch"))
        code, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("no patch file", self.err)
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.github.issue(number)["state"], "open")

    def test_a_patch_that_fails_the_check_gets_no_action(self):
        self.patch("gke-a", "0001-ok.patch", "never; ours")
        for name, retire in (("0002-todo.patch", "TODO"), ("0003-when.patch", "when upstream carries it")):
            path = self.patch("gke-a", name, retire)
            self.github.add_issue(path)
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(out.count("skip     "), 2)
        self.assertEqual(self.github.writes(), [])

    def test_a_general_patch_with_no_commit_from_the_api_is_not_opened(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix", added=None)
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"skip     {path} (the API gave no commit for the file)", out)
        self.assertEqual(self.github.writes(), [])

    def test_each_first_word_that_the_check_accepts_has_a_kind_here(self):
        accepted = {overlay.RETIRE_WHEN_NEVER, *overlay.RETIRE_WHEN_CHANGE_WORDS}
        self.assertEqual(set(reconciler.GENERAL_WORDS) | set(reconciler.SPECIFIC_WORDS), accepted)
        self.assertFalse(set(reconciler.GENERAL_WORDS) & set(reconciler.SPECIFIC_WORDS))


class AssigneeTest(ReconcileCase):
    def assignees_after_a_run(self, **kwargs):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix", **kwargs)
        self.run_script()
        (issue,) = self.github.issues
        return issue["assignees"]

    def test_a_refused_assignee_leaves_the_issue_with_no_assignee(self):
        self.github.refused_assignees.add("author-a")
        self.assertEqual(self.assignees_after_a_run(), [])
        self.assertIn("refused the issue with the assignee author-a", self.err)

    def test_a_robot_author_gives_the_task_to_the_area_owner(self):
        for login in ("kube-agents-robot", "dependabot[bot]", None):
            with self.subTest(login=login):
                self.github = FakeGitHub()
                self.assertEqual(self.assignees_after_a_run(login=login), [{"login": "area-owner"}])

    def test_a_robot_author_and_no_owner_row_gives_no_assignee(self):
        (self.root / "docs" / "ownership.md").write_text("| Area | Key paths | Primary |\n")
        self.assertEqual(self.assignees_after_a_run(login="kube-agents-robot"), [])

    def test_an_owner_cell_that_is_not_one_login_gives_no_assignee(self):
        (self.root / "docs" / "ownership.md").write_text(
            OWNERSHIP.replace("| area-owner |", "| the owner of the area it designs |")
        )
        self.assertEqual(self.assignees_after_a_run(login="kube-agents-robot"), [])

    def test_the_robot_list_has_each_robot_account_of_the_review_request_file(self):
        options = yaml.safe_load((_HERE.parent / ".github" / "auto_request_review.yml").read_text())["options"]
        self.assertTrue(options["robot_accounts"])
        self.assertLessEqual(set(options["robot_accounts"]), set(reconciler.ROBOT_LOGINS))

    def test_the_owner_row_is_in_the_ownership_table_of_this_repository(self):
        with mock.patch.object(overlay, "REPO_ROOT", _HERE.parent):
            owner = reconciler.area_owner()
        self.assertIsNotNone(owner, f"docs/ownership.md has no row `{reconciler.OWNERSHIP_AREA}` with one login as primary")


class IssueLookupTest(ReconcileCase):
    def test_the_issues_are_read_on_all_pages(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        for index in range(100):
            self.github.add_issue(path_of("gke-z", f"{index:04d}-gone.patch"), state="closed")
        self.github.add_issue(path)
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"tracked  {path} (#101 is open)", out)
        self.assertEqual(self.github.writes(), [])

    def test_a_pull_request_with_the_label_is_not_an_issue(self):
        path = self.patch("gke-a", "0001-x.patch", "never; ours")
        number = self.github.add_issue(path)
        self.github.issue(number)["pull_request"] = {}
        self.run_script()
        self.assertEqual(self.github.writes(), [])

    def test_an_issue_with_the_label_and_no_marker_is_not_read(self):
        self.patch("gke-a", "0001-x.patch", "never; ours")
        for body in ("", "<!-- skill-upstream-tracking path=../../etc/passwd -->",
                     "<!-- skill-upstream-tracking path=agents/platform/skill-overlays/gke-a/README.md -->",
                     "see <!-- skill-upstream-tracking path=agents/platform/skill-overlays/gke-a/0001-x.patch --> here",
                     "see <!-- skill-upstream-tracking path=agents/platform/skill-overlays/gke-a/0001-x.patch -->",
                     "<!-- skill-upstream-tracking path=agents/platform/skill-overlayz/gke-a/0001-x.patch -->"):
            self.github.add_issue("unused", body=body)
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(out.count("has the label skills:upstream and no marker"), 6)
        self.assertEqual(self.github.writes(), [])

    def test_a_body_with_crlf_line_ends_keeps_its_marker(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        self.run_script()
        (issue,) = self.github.issues
        issue["body"] = issue["body"].replace("\n", "\r\n")
        self.github.calls.clear()
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"tracked  {path} (#1 is open)", out)
        self.assertEqual(self.github.writes(), [])

    def test_the_last_marker_of_a_body_is_the_identity(self):
        quoted = self.patch("gke-a", "0001-quoted.patch", "upstream carries the fix")
        owner = self.patch("gke-a", "0002-owner.patch", "never; ours")
        body = f"{reconciler.MARKER.format(path=quoted)}\n\nmore text\n\n{reconciler.MARKER.format(path=owner)}"
        number = self.github.add_issue("unused", body=body)
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"close    {owner} (#{number}", out)
        self.assertIn(f"open     {quoted}", out)

    def test_two_issues_with_one_marker_the_open_one_is_the_tracking_issue(self):
        path = self.patch("gke-a", "0001-x.patch", "never; ours")
        second = self.github.add_issue(path, number=2)
        self.github.add_issue(path, state="closed", number=1)
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn(f"{path}: #{second} is the tracking issue; #1 has the same marker", out)
        self.assertEqual(self.github.issue(second)["state"], "closed")
        self.assertEqual(self.github.issue(1).get("state_reason"), None)

    def test_two_closed_issues_with_one_marker_the_oldest_opens_again(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        self.github.add_issue(path, state="closed", number=2)
        self.github.add_issue(path, state="closed", number=1)
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual([issue["state"] for issue in (self.github.issue(1), self.github.issue(2))], ["open", "closed"])

    def test_two_open_issues_with_one_marker_fail_the_run_until_a_person_closes_one(self):
        path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        other = self.patch("gke-b", "0001-other.patch", "upstream carries the fix")
        self.github.add_issue(path, number=2)
        self.github.add_issue(path, number=1)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                code, out = self.run_script(dry_run=dry_run)
                self.assertEqual(code, 1)
                self.assertIn(f"double   {path}: #1 and #2 are open with the same marker", out)
                self.assertIn(f"tracked  {path} (#1 is open)", out)
        # The run did its other work, and it closed no issue that it did not plan to close.
        self.assertEqual([call for call in self.github.writes() if call[0] == "PATCH"], [])
        self.assertEqual(len([issue for issue in self.github.issues if other in issue["body"]]), 1)
        self.assertEqual([self.github.issue(n)["state"] for n in (1, 2)], ["open", "open"])
        self.github.issue(2)["state"] = "closed"
        code, _ = self.run_script()
        self.assertEqual(code, 0)

    def test_each_open_issue_of_a_patch_that_keeps_no_issue_closes_in_one_run(self):
        for retire in ("never; ours", None):
            with self.subTest(retire=retire):
                self.github = FakeGitHub()
                self.patch("gke-a", "0009-other.patch", "never; ours")
                path = self.patch("gke-a", "0001-x.patch", retire) if retire else path_of("gke-a", "0002-gone.patch")
                for number in (3, 5, 9):
                    self.github.add_issue(path, number=number)
                code, out = self.run_script()
                self.assertEqual(code, 0)
                self.assertNotIn("double", out)
                self.assertEqual([self.github.issue(n)["state"] for n in (3, 5, 9)], ["closed"] * 3)
                self.assertEqual([len(self.github.comments[n]) for n in (3, 5, 9)], [1, 1, 1])


class CreateTest(ReconcileCase):
    """The request that opens an issue: a lost answer, a server error and a refusal."""

    ISSUES = ("POST", "/repos/o/r/issues")

    def errors(self):
        return (
            urllib.error.HTTPError("https://api.github.com/x", 502, "bad gateway", {}, None),
            urllib.error.URLError("timed out"),
        )

    def test_a_lost_answer_does_not_open_a_second_issue(self):
        for error in self.errors():
            with self.subTest(error=error):
                self.github = FakeGitHub()
                path = self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
                self.patch("gke-b", "0001-y.patch", "upstream carries the fix")
                self.github.lose_answer.append((*self.ISSUES, error))
                with self.assertRaises(type(error)):
                    self.run_script()
                # GitHub made the issue. The script sent the request one time and stopped the run.
                self.assertEqual(len(self.github.issues), 1)
                self.assertEqual(self.github.calls.count(self.ISSUES), 1)
                code, out = self.run_script()
                self.assertEqual(code, 0)
                self.assertIn(f"tracked  {path} (#1 is open)", out)
                self.assertEqual(len(self.github.issues), 2)

    def test_an_error_before_github_made_the_issue_leaves_it_to_the_next_run(self):
        for error in self.errors():
            with self.subTest(error=error):
                self.github = FakeGitHub()
                self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
                self.github.fail_first.append((*self.ISSUES, error))
                with self.assertRaises(type(error)):
                    self.run_script()
                self.assertEqual(self.github.issues, [])
                self.assertEqual(self.github.calls.count(self.ISSUES), 1)
                code, _ = self.run_script()
                self.assertEqual(code, 0)
                self.assertEqual(len(self.github.issues), 1)

    def test_a_rate_limit_on_the_request_that_opens_an_issue_is_sent_again(self):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        for _ in range(2):
            limit = urllib.error.HTTPError("https://api.github.com/x", 429, "too many requests", {}, None)
            self.github.fail_first.append((*self.ISSUES, limit))
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(self.github.calls.count(self.ISSUES), 3)

    def test_a_refusal_to_open_an_issue_stops_the_run(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                self.github = FakeGitHub()
                for index in range(3):
                    self.patch("gke-a", f"000{index}-x.patch", "upstream carries the fix")
                self.github.refuse[self.ISSUES] = status
                with self.assertRaises(urllib.error.HTTPError):
                    self.run_script()
                self.assertEqual(self.github.calls.count(self.ISSUES), 1)

    def test_a_server_error_on_a_later_request_is_sent_again(self):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        closes = self.patch("gke-b", "0001-y.patch", "never; ours")
        number = self.github.add_issue(closes)
        comments = ("POST", f"/repos/o/r/issues/{number}/comments")
        error = urllib.error.HTTPError("https://api.github.com/x", 502, "bad gateway", {}, None)
        self.github.fail_first.append((*comments, error))
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.github.calls.count(comments), 2)
        self.assertEqual(self.github.issue(number)["state"], "closed")

    def test_a_rate_limit_on_an_action_stops_the_run(self):
        first = self.patch("gke-a", "0001-x.patch", "never; ours")
        number = self.github.add_issue(first)
        self.patch("gke-b", "0001-y.patch", "upstream carries the fix")
        self.github.refuse[("POST", f"/repos/o/r/issues/{number}/comments")] = 429
        with self.assertRaises(urllib.error.HTTPError):
            self.run_script()
        self.assertEqual(self.github.calls.count(self.ISSUES), 0)

    def test_a_second_run_that_opens_an_issue_finds_the_label(self):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        self.run_script()
        self.patch("gke-a", "0002-y.patch", "upstream carries the fix")
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.github.issues), 2)

    def test_a_refused_action_does_not_stop_the_other_actions(self):
        locked = self.patch("gke-a", "0001-locked.patch", "never; ours")
        number = self.github.add_issue(locked)
        self.github.refuse[("POST", f"/repos/o/r/issues/{number}/comments")] = 403
        self.patch("gke-b", "0001-x.patch", "upstream carries the fix")
        for _ in range(2):
            code, out = self.run_script()
            self.assertEqual(code, 1)
            self.assertIn(f"refused  {locked}: GitHub answered 403", self.err)
        self.assertEqual(self.github.issue(number)["state"], "open")
        self.assertEqual(len(self.github.issues), 2)
        self.assertIn("Done: 0 actions. GitHub refused 1.", out)

    def test_a_path_that_the_marker_cannot_hold_gets_no_issue(self):
        self.patch("gke-a", "0001-ok.patch", "never; ours")
        spaced = self.patch("gke-a", "0002-my change.patch", "upstream carries the fix")
        folder = self.patch("gke_b", "0001-x.patch", "upstream carries the fix")
        for _ in range(2):
            code, out = self.run_script()
            self.assertEqual(code, 0)
        self.assertIn(f"skip     {spaced} (the path cannot be the identity of an issue", out)
        self.assertIn(f"skip     {folder} (the path cannot be the identity of an issue", out)
        self.assertEqual(self.github.writes(), [])


class MainTest(ReconcileCase):
    def run_main(self, argv, env):
        out = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = reconciler.main(argv, opener=self.github.opener, now=NOW)
        self.err = err.getvalue()
        return code, out.getvalue()

    def test_a_run_that_writes_refuses_a_ref_that_is_not_main(self):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        for ref in ("refs/heads/a-branch", "refs/pull/1/merge", "refs/tags/main", ""):
            with self.subTest(ref=ref):
                code, _ = self.run_main(["--repo", REPO], {"GITHUB_TOKEN": "t", "GITHUB_REF": ref})
                self.assertEqual(code, 1)
                self.assertIn("needs the ref refs/heads/main", self.err)
                self.assertEqual(self.github.calls, [])

    def test_a_run_that_writes_goes_on_main(self):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        code, _ = self.run_main(["--repo", REPO], {"GITHUB_TOKEN": "t", "GITHUB_REF": "refs/heads/main"})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.github.issues), 1)

    def test_a_dry_run_goes_on_each_ref(self):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix")
        code, out = self.run_main(["--repo", REPO, "--dry-run"], {"GH_TOKEN": "t", "GITHUB_REF": "refs/heads/a-branch"})
        self.assertEqual(code, 0)
        self.assertIn("Dry run: 1 actions, nothing written.", out)
        self.assertEqual(self.github.issues, [])

    def test_no_token_is_a_failure(self):
        code, _ = self.run_main(["--dry-run"], {})
        self.assertEqual(code, 1)
        self.assertIn("is not set", self.err)

    def test_a_refusal_of_github_is_a_failure_with_a_message(self):
        self.patch("gke-a", "0001-x.patch", "upstream carries the fix")

        def refuse(request):
            raise urllib.error.HTTPError(request.full_url, 403, "refused", {}, None)

        self.github.opener = refuse
        code, _ = self.run_main(["--repo", REPO, "--dry-run"], {"GITHUB_TOKEN": "t"})
        self.assertEqual(code, 1)
        self.assertIn("the run stopped", self.err)


class RealTreeTest(unittest.TestCase):
    """The patches of this checkout, with stand-ins for the API answers."""

    def test_a_dry_run_lists_each_general_patch_of_this_checkout(self):
        github = FakeGitHub()
        states = reconciler.patch_states()
        general = sorted(path for path, (kind, _) in states.items() if kind == reconciler.GENERAL)
        self.assertTrue(general)
        self.assertEqual([path for path, (kind, _) in states.items() if kind == reconciler.UNREADABLE], [])
        for path in general:
            github.commits[path] = [commit("a", OLD)]
        api = reconciler.GitHubAPI(REPO, "t", dry_run=True, opener=github.opener)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reconciler.reconcile(api, REPO, NOW, True)
        self.assertEqual(code, 0)
        listed = sorted(line.split()[1] for line in out.getvalue().splitlines() if line.startswith("open     "))
        self.assertEqual(listed, general)
        self.assertEqual({method for method, _ in github.calls}, {"GET"})


if __name__ == "__main__":
    unittest.main()
