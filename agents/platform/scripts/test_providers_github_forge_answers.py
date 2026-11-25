"""A GitHub answer with a value of a wrong type does not end a verb in a traceback.

    python3 -m unittest discover -s agents/platform/scripts -p test_providers_github_forge_answers.py

The forge's answer is another party's text. Two places of `providers/github/forge.py`
use a raw value of it before the translation: the filter for a review summary reads the
review's `body`, and the labelled proposal listing reads each hit's `number`.
"""

from __future__ import annotations

import unittest
from typing import Any

from providers import AVAILABLE
from providers.validate import MAX_PAGE_SIZE
from workspace_paths import WorkspaceError

FORGE_NAME = "github"
REPO = "acme/infra"
PROPOSAL = 7
LABEL = "stream"
# How the forge names a listing that it cannot answer.
GATEWAY_STATUS = 502
FORGE_CALL_FAILED = "FORGE_CALL_FAILED"
NOT_A_TEXT = ([["nested"]], {"a": "b"}, 7, 1.5, True)
NOT_A_NUMBER = ([5], {"n": 5}, "5", 5.5, True, False, None)
# Numbers of pull requests that fill a page and that no test asks for.
OTHER_PULLS_FROM = 1000


def github_forge() -> Any:
    """The GitHub forge as an install with no forge configuration builds it, through the package surface."""
    (cls,) = [cls for cls in AVAILABLE if cls.name == FORGE_NAME]
    (forge,) = list(cls.for_config({}))
    return forge


def pull(number: Any, title: str = "a title") -> dict[str, Any]:
    return {"number": number, "title": title, "state": "open", "labels": [], "head": {"ref": "work"}, "base": {"ref": "main"}}


def hit(number: Any) -> dict[str, Any]:
    """A row of the issues endpoint that is a pull request."""
    return {"number": number, "pull_request": {}}


def review(ident: int, body: Any) -> dict[str, Any]:
    return {"id": ident, "user": {"login": "reviewer"}, "submitted_at": f"2026-11-01T00:00:0{ident}Z", "body": body}


class Answers:
    """The transport's `api`: one answer for each path, a list of pages where the forge reads pages."""

    def __init__(self, by_path: dict[str, Any]) -> None:
        self.by_path = by_path
        self.calls: list[str] = []

    def __call__(self, method: str, path: str, *, params: dict | None = None, **_: Any) -> Any:
        self.calls.append(path)
        if path not in self.by_path:
            raise AssertionError(f"the forge made a call with no answer: {method} {path}")
        answer = self.by_path[path]
        if isinstance(answer, Pages):
            return answer.page((params or {}).get("page", 1))
        return answer


class Pages:
    def __init__(self, *pages: list) -> None:
        self.pages = pages

    def page(self, number: int) -> list:
        return self.pages[number - 1] if number <= len(self.pages) else []


class ReviewSummaryTest(unittest.TestCase):
    def view(self, reviews: list) -> dict[str, Any]:
        api = Answers(
            {
                f"repos/{REPO}/pulls/{PROPOSAL}": pull(PROPOSAL),
                f"repos/{REPO}/issues/{PROPOSAL}/comments": [],
                f"repos/{REPO}/pulls/{PROPOSAL}/comments": [],
                f"repos/{REPO}/pulls/{PROPOSAL}/reviews": reviews,
            }
        )
        return github_forge().proposal_view(api, REPO, {"number": PROPOSAL, "comments": True})

    def test_a_review_whose_body_is_not_a_text_is_left_out_and_the_view_answers(self):
        for value in NOT_A_TEXT:
            with self.subTest(body=value):
                answer = self.view([review(1, "please fix this"), review(2, value), review(3, "and this")])
                self.assertEqual(["please fix this", "and this"], [c["body"] for c in answer["comments"]])
                self.assertEqual(2, answer["commentCount"])

    def test_a_review_with_a_text_stays_and_one_with_no_text_is_left_out_as_before(self):
        no_body = {"id": 6, "user": {"login": "reviewer"}, "submitted_at": "2026-11-01T00:00:06Z"}
        answer = self.view(
            [review(1, "please fix this"), review(2, None), review(3, ""), review(4, "  \n"), review(5, "0"), no_body]
        )
        self.assertEqual([("review", "please fix this"), ("review", "0")], [(c["kind"], c["body"]) for c in answer["comments"]])


class LabelledListingTest(unittest.TestCase):
    def labelled(self, by_path: dict[str, Any]) -> tuple[dict[str, Any], Answers]:
        api = Answers(by_path)
        return github_forge().proposal_list(api, REPO, {"labels": [LABEL]}), api

    def test_a_hit_whose_number_is_not_an_integer_fails_the_listing_by_name(self):
        """A hit that is left out reads as "no pull request" to a caller that lists them
        all, so the listing does not answer with the rest."""
        for value in (*NOT_A_NUMBER, "absent"):
            with self.subTest(number=value):
                bad = {"pull_request": {}} if value == "absent" else hit(value)
                with self.assertRaises(WorkspaceError) as refused:
                    self.labelled({f"repos/{REPO}/issues": [hit(5), bad, hit(6)]})
                self.assertEqual((GATEWAY_STATUS, FORGE_CALL_FAILED), (refused.exception.status, refused.exception.fields.get("code")))
                self.assertIn("not an integer", str(refused.exception))

    def test_an_issue_with_no_usable_number_is_no_hit_and_does_not_fail_the_listing(self):
        """A row with no `pull_request` is an issue: its number is not read."""
        answer, _ = self.labelled(
            {
                f"repos/{REPO}/issues": [hit(5), {"number": [9]}, {"title": "an issue"}, hit(6)],
                f"repos/{REPO}/pulls": Pages([pull(6, "six"), pull(5, "five")]),
            }
        )
        self.assertEqual([(5, "five"), (6, "six")], [(p["number"], p["title"]) for p in answer["proposals"]])

    def test_a_pull_request_whose_number_is_not_an_integer_is_not_one_of_the_wanted(self):
        """In the scan of `/pulls`, a `number` that is `True` is not pull request 1, and a
        list or an object does not end the scan."""
        for value in NOT_A_NUMBER:
            with self.subTest(number=value):
                answer, _ = self.labelled(
                    {
                        f"repos/{REPO}/issues": [hit(1), hit(2)],
                        f"repos/{REPO}/pulls": Pages([pull(value, "not wanted"), pull(2, "two"), pull(1, "one")]),
                    }
                )
                self.assertEqual([(1, "one"), (2, "two")], [(p["number"], p["title"]) for p in answer["proposals"]])

    def test_a_full_page_that_ends_in_a_number_of_a_wrong_type_ends_the_scan_as_an_absent_number_does(self):
        """The scan stops when a page runs older than the oldest number wanted. A last row
        with no usable number reads as an absent one did: the scan stops after that page
        (four numbers wanted allow two pages), and each number still wanted is read by itself."""
        wanted = (5, 6, 7, 8)
        for value in (*NOT_A_NUMBER, "absent"):
            with self.subTest(number=value):
                last = {"title": "no number"} if value == "absent" else pull(value)
                page = [pull(OTHER_PULLS_FROM + i) for i in range(MAX_PAGE_SIZE - 1)] + [last]
                answers: dict[str, Any] = {
                    f"repos/{REPO}/issues": [hit(number) for number in wanted],
                    f"repos/{REPO}/pulls": Pages(page, page),
                }
                answers.update({f"repos/{REPO}/pulls/{number}": pull(number, f"pull {number}") for number in wanted})
                answer, api = self.labelled(answers)
                self.assertEqual(
                    [(number, f"pull {number}") for number in wanted],
                    [(p["number"], p["title"]) for p in answer["proposals"]],
                )
                self.assertEqual(1, api.calls.count(f"repos/{REPO}/pulls"))

    def test_a_full_page_that_ends_in_a_newer_number_goes_on_to_the_next_page(self):
        """The control for the test above: a last row with an integer above the oldest
        number wanted does not end the scan, and no number is read by itself."""
        wanted = (5, 6, 7, 8)
        first = [pull(OTHER_PULLS_FROM + i) for i in range(MAX_PAGE_SIZE)]
        second = [pull(number, f"pull {number}") for number in reversed(wanted)]
        answer, api = self.labelled(
            {
                f"repos/{REPO}/issues": [hit(number) for number in wanted],
                f"repos/{REPO}/pulls": Pages(first, second),
            }
        )
        self.assertEqual([(number, f"pull {number}") for number in wanted], [(p["number"], p["title"]) for p in answer["proposals"]])
        self.assertEqual([f"repos/{REPO}/issues", f"repos/{REPO}/pulls", f"repos/{REPO}/pulls"], api.calls)

    def test_the_numbers_that_github_sends_list_as_before(self):
        answer, api = self.labelled(
            {
                f"repos/{REPO}/issues": [hit(5), {"number": 9}, hit(6)],
                f"repos/{REPO}/pulls": Pages([pull(6, "six"), pull(5, "five")]),
            }
        )
        self.assertEqual([(5, "five"), (6, "six")], [(p["number"], p["title"]) for p in answer["proposals"]])
        self.assertEqual({"proposals", "count", "truncated"}, set(answer))
        self.assertEqual([f"repos/{REPO}/issues", f"repos/{REPO}/pulls"], api.calls)


if __name__ == "__main__":
    unittest.main()
