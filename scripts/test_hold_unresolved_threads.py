#!/usr/bin/env python3
"""Tests for hold_unresolved_threads.py -- the label that keeps an unmergeable PR out of Tide's pool.

Run: cd scripts && python3 -m unittest test_hold_unresolved_threads

Both wrong answers are quiet. A pull request that should have been labelled
sits in the pool and Tide retries it every 85 seconds ahead of everyone else;
a label that should have been left alone -- a person's, or one on a pull
request whose threads are still open -- disappears and the merge Tide then
attempts is the one the label existed to prevent. So each side of `decide`
gets a case, and the ownership check is driven with the timeline shapes the
API actually returns.
"""

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import hold_unresolved_threads as hold

REPO = "gke-labs/kube-agents"
POOL = {"lgtm", "approved", "size/L"}


def pull(number, labels=POOL, draft=False):
    return {"number": number, "draft": draft, "labels": set(labels)}


class FakeAPI:
    """Just enough of `GitHubAPI` for the functions that call it."""

    def __init__(self, pulls=(), threads=None, timelines=None, comments=None):
        self.repo = REPO
        self.pulls = list(pulls)
        self.threads = threads or {}  # number -> list of pages, each a list of isResolved flags
        self.timelines = timelines or {}
        self.comments = comments or {}
        self.posts = []
        self.deletes = []

    def graphql(self, query, variables):
        pages = self.threads.get(variables["number"], [[]])
        index = int(variables["after"] or 0)
        page = pages[index]
        more = index + 1 < len(pages)
        return {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": more, "endCursor": str(index + 1) if more else None},
                        "nodes": [{"isResolved": flag} for flag in page],
                    }
                }
            }
        }

    def get_all(self, path):
        if path.endswith("/pulls?state=open"):
            return [
                {"number": p["number"], "draft": p["draft"], "labels": [{"name": n} for n in p["labels"]]}
                for p in self.pulls
            ]
        number = int(path.rsplit("/issues/", 1)[1].split("/")[0])
        if path.endswith("/timeline"):
            return self.timelines.get(number, [])
        if path.endswith("/comments"):
            return [{"body": body} for body in self.comments.get(number, [])]
        raise AssertionError(path)

    def post(self, path, payload, tolerate=()):
        self.posts.append((path, payload))

    def delete(self, path, tolerate=()):
        self.deletes.append(path)


def labeled(by, name=hold.LABEL):
    return {"event": "labeled", "label": {"name": name}, "actor": {"login": by}}


class DecideTest(unittest.TestCase):
    def test_unresolved_threads_in_the_pool_get_the_label(self):
        self.assertEqual(hold.decide(pull(1), 3)[0], hold.ACTION_ADD)

    def test_unresolved_threads_already_labelled_are_left_alone(self):
        self.assertIsNone(hold.decide(pull(1, POOL | {hold.LABEL}), 3)[0])

    def test_a_draft_is_not_in_the_pool(self):
        self.assertIsNone(hold.decide(pull(1, draft=True), 3)[0])

    def test_without_both_pool_labels_there_is_nothing_to_keep_out(self):
        self.assertIsNone(hold.decide(pull(1, {"approved"}), 3)[0])

    def test_all_resolved_and_labelled_is_removed(self):
        self.assertEqual(hold.decide(pull(1, POOL | {hold.LABEL}), 0)[0], hold.ACTION_REMOVE)

    def test_the_label_comes_off_even_after_the_pool_labels_went(self):
        """A push stripped `lgtm`; the label must not outlive the threads it was for."""
        self.assertEqual(hold.decide(pull(1, {hold.LABEL}), 0)[0], hold.ACTION_REMOVE)

    def test_all_resolved_and_unlabelled_is_nothing(self):
        self.assertIsNone(hold.decide(pull(1), 0)[0])


class SelectionTest(unittest.TestCase):
    def test_only_the_pool_and_the_labelled_are_read(self):
        """Every other open pull request costs a thread query it does not need."""
        api = FakeAPI(pulls=[pull(1, {"lgtm"}), pull(2), pull(3, {hold.LABEL}), pull(4, POOL, draft=True)])
        chosen = sorted(n for n, p in hold.open_pulls(api).items() if hold.concerns_us(p))
        self.assertEqual(chosen, [2, 3])


class OwnershipTest(unittest.TestCase):
    def test_the_last_application_decides(self):
        api = FakeAPI(timelines={1: [labeled("someone"), labeled(hold.BOT_LOGIN)]})
        self.assertTrue(hold.label_is_ours(api, 1))
        api = FakeAPI(timelines={1: [labeled(hold.BOT_LOGIN), labeled("someone")]})
        self.assertFalse(hold.label_is_ours(api, 1))

    def test_other_labels_do_not_count(self):
        api = FakeAPI(timelines={1: [labeled(hold.BOT_LOGIN, "risk:low")]})
        self.assertFalse(hold.label_is_ours(api, 1))

    def test_no_event_at_all_is_not_ours(self):
        self.assertFalse(hold.label_is_ours(FakeAPI(), 1))


class ThreadCountTest(unittest.TestCase):
    def test_every_page_is_counted(self):
        api = FakeAPI(threads={1: [[True] * 3, [True, False], [False]]})
        self.assertEqual(hold.unresolved_threads(api, 1), 2)


class SweepTest(unittest.TestCase):
    def test_labels_and_comments_once(self):
        api = FakeAPI(pulls=[pull(7)], threads={7: [[False, True]]})
        lines, failures = hold.sweep(api)
        self.assertEqual(failures, 0)
        paths = [path for path, _ in api.posts]
        self.assertIn(f"/repos/{REPO}/issues/7/labels", paths)
        self.assertIn(f"/repos/{REPO}/issues/7/comments", paths)
        comment = next(payload for path, payload in api.posts if path.endswith("/comments"))["body"]
        self.assertTrue(comment.startswith(hold.COMMENT_MARKER))
        self.assertIn("1 review thread is unresolved", comment)
        self.assertIn("#7: labelled", lines[0])

    def test_a_relabel_does_not_repeat_the_comment(self):
        """Someone removed the label by hand with threads still open: label back, comment not."""
        api = FakeAPI(pulls=[pull(7)], threads={7: [[False]]}, comments={7: [hold.COMMENT_MARKER + "\nearlier"]})
        lines, _ = hold.sweep(api)
        paths = [path for path, _ in api.posts]
        self.assertIn(f"/repos/{REPO}/issues/7/labels", paths)
        self.assertNotIn(f"/repos/{REPO}/issues/7/comments", paths)
        self.assertIn("comment is already there", lines[0])

    def test_the_label_is_created_before_it_is_applied(self):
        api = FakeAPI(pulls=[pull(7)], threads={7: [[False]]})
        hold.sweep(api)
        self.assertEqual(api.posts[0][0], f"/repos/{REPO}/labels")
        self.assertEqual(api.posts[0][1]["name"], hold.LABEL)

    def test_removes_its_own_label_once_resolved(self):
        api = FakeAPI(
            pulls=[pull(7, POOL | {hold.LABEL})],
            threads={7: [[True, True]]},
            timelines={7: [labeled(hold.BOT_LOGIN)]},
        )
        hold.sweep(api)
        self.assertEqual(api.deletes, [f"/repos/{REPO}/issues/7/labels/{hold.LABEL}"])

    def test_leaves_a_persons_label_alone(self):
        api = FakeAPI(
            pulls=[pull(7, POOL | {hold.LABEL})],
            threads={7: [[True]]},
            timelines={7: [labeled("a-maintainer")]},
        )
        lines, _ = hold.sweep(api)
        self.assertEqual(api.deletes, [])
        self.assertIn("applied by a person", lines[0])

    def test_dry_run_writes_nothing(self):
        api = FakeAPI(
            pulls=[pull(7), pull(8, POOL | {hold.LABEL})],
            threads={7: [[False]], 8: [[True]]},
            timelines={8: [labeled(hold.BOT_LOGIN)]},
        )
        lines, _ = hold.sweep(api, dry_run=True)
        self.assertEqual(api.posts, [])
        self.assertEqual(api.deletes, [])
        self.assertIn("#7: would be labelled", lines[0])
        self.assertIn("#8: would be unlabelled", lines[1])

    def test_one_failure_does_not_stop_the_sweep(self):
        api = FakeAPI(pulls=[pull(7), pull(8)], threads={8: [[False]]})
        original = api.graphql

        def graphql(query, variables):
            if variables["number"] == 7:
                raise RuntimeError("boom")
            return original(query, variables)

        api.graphql = graphql
        lines, failures = hold.sweep(api)
        self.assertEqual(failures, 1)
        self.assertIn("#7: failed", lines[0])
        self.assertIn("#8: labelled", lines[1])


if __name__ == "__main__":
    unittest.main()
