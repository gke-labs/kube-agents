#!/usr/bin/env python3
"""Tests for the readings sweep (`vcs_readings.py`) against a fake forge."""

from __future__ import annotations

import datetime
import math
import os
import threading
import unittest
from unittest import mock

import providers
from providers import ForgeUnsupported, IdentityUnknown, WorkspaceError

import vcs_readings
from vcs_readings import (
    ProposalRecord,
    ReadingsSweep,
    Touch,
    bucket_counts,
    classify_touch,
    install_commit_emails,
)

INSTALL = "kube-agents[bot]"
INSTALL_BARE = "kube-agents"
INSTALL_EMAIL = "platform-agent@kube-agents.invalid"
REPO = "fake:github.com/acme/infra"
OTHER_REPO = "fake:github.com/acme/apps"
GITEA_REPO = "gitea:gitea.example.com/acme/infra"
GITHUB_REPO = "github:github.com/acme/infra"
LOGGER_NAME = "credential-proxy.vcs-readings"


def iso(seconds_ago: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


RECENT = iso(3600)
OLD = "2020-01-01T00:00:00Z"


def rate_limited() -> WorkspaceError:
    return WorkspaceError("slow down", status=429, code="FORGE_RATE_LIMITED")


class FakeTransport:
    def __init__(self, forge: "FakeForge"):
        self.forge = forge

    def whoami(self) -> str:
        self.forge.whoami_calls += 1
        if isinstance(self.forge.login, Exception):
            raise self.forge.login
        return self.forge.login

    def api(self, method, path, **kwargs):
        self.forge.calls.append(path)
        if path == "user":
            return {"id": 7, "login": self.forge.login}
        return None


class FakeForge:
    """Answers the sweep's six reads from in-memory proposals and events."""

    COMMIT_CAP = 250

    def __init__(self, host="github.com", login=INSTALL, clamp=None, splits=True, http_user=False):
        self.name = "fake"
        self.hosts = (host,)
        self.whoami_graphql = "" if http_user else f"https://{host}/graphql"
        self.whoami_route = ("user", "login") if http_user else None
        self.login = login
        self.clamp = clamp
        self.splits = splits
        self.proposals: dict[int, dict] = {}
        self.reviews: dict[int, list] = {}
        self.comments: dict[int, list] = {}
        self.commits: dict[int, list] = {}
        self.closers: dict[int, dict] = {}
        self.fail: dict[str, object] = {}
        self.calls: list[str] = []
        self.whoami_calls = 0
        self.list_payloads: list[dict] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        # Called with the page number before each listing page is served.
        self.before_page = None
        # Proposals whose reviews and comments run past the conversation bound.
        self.long_conversations: set[int] = set()

    def add(self, number, state="open", author=INSTALL_BARE, created=RECENT, updated=None, **events):
        self.proposals[number] = {
            "number": number,
            "state": state,
            "author": author,
            "authorBot": True,
            "created": created,
            "updated": updated or f"2026-10-{number % 28 + 1:02d}T00:00:00Z#{number}",
            "closed": "",
        }
        for kind in ("reviews", "comments", "commits"):
            getattr(self, kind)[number] = list(events.get(kind, []))
        if "closer" in events:
            self.closers[number] = events["closer"]

    def _call(self, api, verb, number=None):
        api("GET", verb)
        failure = self.fail.get(verb)
        if callable(failure):
            failure = failure(number)
        if isinstance(failure, Exception):
            raise failure

    def proposal_list_by_author(self, api, repo, payload):
        self.list_payloads.append(dict(payload))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait()
        self._call(api, "list")
        if self.before_page is not None:
            self.before_page(payload["page"])
        items = sorted(self.proposals.values(), key=lambda p: p["updated"], reverse=True)
        served = min(payload["limit"], self.clamp or payload["limit"])
        start = (payload["page"] - 1) * served
        batch = [dict(p) for p in items[start : start + served]]
        return {
            "proposals": batch,
            "count": len(batch),
            "truncated": start + len(batch) < len(items),
            "total": len(items),
        }

    def proposal_tally(self, api, repo, payload):
        self._call(api, "tally")
        states = [p["state"] for p in self.proposals.values()]
        if not self.splits:
            return {"open": states.count("open"), "merged": None, "closed": None}
        return {state: states.count(state) for state in ("open", "merged", "closed")}

    def proposal_reviews(self, api, repo, payload):
        self._call(api, "reviews", payload["number"])
        reviews = self.reviews.get(payload["number"], [])
        truncated = payload["number"] in self.long_conversations
        return {"reviews": reviews, "count": len(reviews), "truncated": truncated}

    def proposal_view(self, api, repo, payload):
        self._call(api, "view", payload["number"])
        assert payload["comments"] is True
        comments = self.comments.get(payload["number"], [])
        return {
            "proposal": dict(self.proposals[payload["number"]]),
            "comments": comments,
            "commentCount": len(comments),
            "commentsTruncated": payload["number"] in self.long_conversations,
        }

    def proposal_commits(self, api, repo, payload):
        self._call(api, "commits", payload["number"])
        rows = self.commits.get(payload["number"], [])[: self.COMMIT_CAP]
        start = (payload["page"] - 1) * payload["limit"]
        batch = rows[start : start + payload["limit"]]
        return {"commits": batch, "count": len(batch), "truncated": len(batch) >= payload["limit"]}

    def proposal_closer(self, api, repo, payload):
        self._call(api, "closer", payload["number"])
        return self.closers.get(payload["number"], {"actor": "", "bot": False})

    def detail_calls(self) -> int:
        return sum(1 for path in self.calls if path in ("reviews", "view", "commits", "closer"))


class FakeRegistry:
    """`providers.Registry.resolve` over `host/path`, keyed by managed key.

    A managed key handed whole, `provider:` and all, is refused the way the
    real registry refuses it: the text before the first `/` reads as a host no
    forge serves.
    """

    def __init__(self, forges: dict[str, FakeForge]):
        self.forges = forges

    def resolve(self, locator):
        host, _, path = locator.partition("/")
        if ":" in host:
            raise ForgeUnsupported(f"{host} is not a forge this install serves")
        for key, forge in self.forges.items():
            if key.partition(":")[2] == locator:
                return forge, path
        raise ForgeUnsupported(f"{host} is not a forge this install serves")


class FakeMetrics:
    def __init__(self):
        self.readings: list[tuple] = []
        self.failures: list[str] = []

    def set_readings(self, outcomes, details):
        self.readings.append((outcomes, details))

    def record_reading_failure(self, reason):
        self.failures.append(reason)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def review(state, author="alice", bot=False):
    return {"id": 1, "state": state, "author": author, "bot": bot, "created": RECENT}


def comment(kind, author="alice", bot=False, body="looks fine"):
    return {"kind": kind, "author": author, "bot": bot, "body": body}


def commit(author, email, bot=False):
    return {"author": author, "email": email, "bot": bot}


def install_commit():
    # The install's own commit, as a real forge returns it: no linked login,
    # so the translator falls back to the git name, and the install's address.
    return commit("Platform Agent", INSTALL_EMAIL)


class Harness:
    def __init__(
        self, forges: dict[str, FakeForge], interval=vcs_readings.VCS_READINGS_TTL_SECONDS, app_credential=None
    ):
        self.forges = forges
        self.metrics = FakeMetrics()
        self.clock = FakeClock()
        self.sweep = ReadingsSweep(
            registry=FakeRegistry(forges),
            transport_for=lambda forge, repo: FakeTransport(forge),
            metrics=self.metrics,
            managed_repositories=lambda: frozenset(forges),
            clock=self.clock,
            interval=interval,
            app_credential=app_credential,
        )

    def last(self):
        return self.metrics.readings[-1]


# An interval long enough that the per-sweep share of the hourly detail
# budget covers a thousand proposals, for tests that are not about the budget.
WIDE = 3600 * 10


class ReadingsSweepTest(unittest.TestCase):
    def test_a_full_sweep_publishes_every_series_and_bucket(self):
        forge = FakeForge()
        forge.add(1, "open", reviews=[review("commented", author="carol")], commits=[install_commit()])
        forge.add(
            2,
            "merged",
            comments=[comment("issue", author=INSTALL_BARE, bot=True)],
            commits=[install_commit()],
        )
        forge.add(
            3,
            "merged",
            reviews=[review("approved"), review("changes_requested", author="renovate", bot=True)],
            comments=[
                comment("issue", author="dependabot", bot=True),
                comment("review_comment", author="bob"),
                comment("review", author="alice", body="a review's body is not a second touch"),
            ],
            commits=[install_commit()],
        )
        forge.add(
            4,
            "closed",
            commits=[commit("dave", "dave@example.com")],
            closer={"actor": "erin", "bot": False},
        )
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.merged, outcomes.closed), (1, 2, 1))
        self.assertEqual(details.closed_by, {"install": 0, "human": 1, "bot": 0})
        self.assertEqual(
            details.touches,
            {
                Touch("review_commented", "human"): 1,
                Touch("review_approved", "human"): 1,
                Touch("review_changes_requested", "bot"): 1,
                Touch("comment", "bot"): 1,
                Touch("review_comment", "human"): 1,
                Touch("commit", "human"): 1,
            },
        )
        inf = float("inf")
        self.assertEqual(
            details.buckets["merged"],
            {0.0: 1, 1.0: 1, 2.0: 2, 3.0: 2, 5.0: 2, 8.0: 2, 13.0: 2, 21.0: 2, inf: 2},
        )
        self.assertEqual(details.buckets["closed"][0.0], 0)
        self.assertEqual(details.buckets["closed"][1.0], 1)
        self.assertEqual(details.buckets["closed"][inf], 1)
        self.assertEqual(details.ignored, 0)
        self.assertEqual(list(details.buckets["merged"])[-1], inf)
        self.assertEqual(harness.metrics.failures, [])
        self.assertGreater(outcomes.taken_at, 0)

    def test_the_installs_unlinked_commit_is_not_a_touch(self):
        emails = install_commit_emails("github.com", INSTALL, None)
        self.assertIsNone(
            classify_touch({"type": "commit", "author": None, "email": INSTALL_EMAIL, "bot": False}, INSTALL, emails)
        )
        self.assertIsNone(
            classify_touch(
                {"type": "commit", "author": "Platform Agent", "email": "platform-agent@users.noreply.github.com", "bot": False},
                INSTALL,
                emails,
            )
        )
        self.assertEqual(
            classify_touch({"type": "commit", "author": None, "email": "dave@example.com", "bot": False}, INSTALL, emails),
            Touch("commit", "human"),
        )
        with mock.patch.dict(os.environ, {"GITHUB_APP_ID": "42", "CREDENTIAL_PROXY_GIT_AUTHOR_EMAIL": "Agent@Corp.example"}):
            emails = install_commit_emails("github.com", INSTALL, os.environ["GITHUB_APP_ID"])
        self.assertIn("42+kube-agents[bot]@users.noreply.github.com", emails)
        self.assertIn("agent@corp.example", emails)
        self.assertIsNone(
            classify_touch({"type": "commit", "author": "x", "email": "agent@corp.example", "bot": False}, INSTALL, emails)
        )

        forge = FakeForge()
        forge.add(1, "merged", commits=[install_commit(), install_commit()])
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        _, details = harness.last()
        self.assertEqual(details.touches, {})
        self.assertEqual(details.buckets["merged"][0.0], 1)

    def test_a_slash_agent_comment_is_agent_address_not_comment(self):
        emails = install_commit_emails("github.com", INSTALL, None)
        self.assertEqual(
            classify_touch({"type": "comment", "kind": "issue", "author": "alice", "bot": False, "body": "  /agent rebase"}, INSTALL, emails),
            Touch("agent_address", "human"),
        )
        self.assertEqual(
            classify_touch({"type": "comment", "kind": "issue", "author": "alice", "bot": False, "body": "please /agent"}, INSTALL, emails),
            Touch("comment", "human"),
        )
        self.assertEqual(
            classify_touch({"type": "comment", "kind": "review_comment", "author": "alice", "bot": False, "body": "/agent x"}, INSTALL, emails),
            Touch("review_comment", "human"),
        )
        self.assertIsNone(
            classify_touch({"type": "comment", "kind": "issue", "author": INSTALL_BARE, "bot": True, "body": "/agent"}, INSTALL, emails)
        )

        forge = FakeForge()
        forge.add(1, "open", comments=[comment("issue", body="/agent please retry")])
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        _, details = harness.last()
        self.assertEqual(details.touches, {Touch("agent_address", "human"): 1})

    def test_an_open_proposal_older_than_fourteen_days_with_no_touch_is_ignored(self):
        forge = FakeForge()
        forge.add(1, "open", created=OLD)
        forge.add(2, "open", created=OLD, reviews=[review("approved", author="renovate", bot=True)])
        forge.add(3, "open", created=OLD, comments=[comment("issue")])
        forge.add(4, "open", created=RECENT)
        forge.add(5, "closed", created=OLD)
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        _, details = harness.last()
        # 1 untouched and 2 touched by a bot only; 3 has a human, 4 is young, 5 is closed.
        self.assertEqual(details.ignored, 2)

    def test_a_foreign_author_in_the_listing_ends_the_repository_with_reason_forge(self):
        forge = FakeForge()
        forge.add(1, "merged")
        forge.add(2, "merged", author="someone-else")
        harness = Harness({REPO: forge})

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        self.assertEqual(harness.metrics.readings, [])
        self.assertEqual(harness.metrics.failures, ["forge"])
        self.assertEqual(forge.detail_calls(), 0)

    def test_a_truncated_github_repository_publishes_exact_state_counts_from_the_tally(self):
        forge = FakeForge()
        for number in range(1, 1102):
            forge.add(number, "merged" if number % 3 else "closed", updated=f"2026-01-01T00:00:00Z{number:05d}")
        forge.add(1102, "open", updated="2026-01-01T00:00:00Z00000")
        harness = Harness({REPO: forge}, interval=WIDE)

        harness.sweep.run_once()

        self.assertEqual(forge.calls.count("list"), vcs_readings.VCS_READINGS_MAX_PAGES)
        self.assertEqual(forge.calls.count("tally"), 1)
        outcomes, details = harness.last()
        states = [p["state"] for p in forge.proposals.values()]
        self.assertEqual(
            (outcomes.open, outcomes.merged, outcomes.closed),
            (states.count("open"), states.count("merged"), states.count("closed")),
        )
        self.assertEqual(outcomes.open, 1)
        # Details cover the thousand most recently updated proposals.
        inf = float("inf")
        self.assertEqual(details.buckets["merged"][inf] + details.buckets["closed"][inf], 1000)
        self.assertEqual(harness.metrics.failures, ["truncated"])

        # The record of a proposal that fell off the end of the walk is kept.
        newest = max(forge.proposals)
        forge.add(newest + 1, "merged", updated="2026-12-31T00:00:00Z")
        harness.sweep.run_once()
        _, details = harness.last()
        self.assertEqual(details.buckets["merged"][inf] + details.buckets["closed"][inf], 1001)

    def test_a_truncated_gitea_repository_withholds_the_outcomes_reading(self):
        forge = FakeForge(host="gitea.example.com", login="kube-agents", clamp=50, splits=False, http_user=True)
        for number in range(1, 1102):
            forge.add(number, "merged", updated=f"2026-01-01T00:00:00Z{number:05d}")
        harness = Harness({"fake:gitea.example.com/acme/infra": forge}, interval=WIDE)

        harness.sweep.run_once()

        # Bounded in proposals under a server that clamps the page size.
        self.assertEqual(forge.calls.count("list"), vcs_readings.VCS_READINGS_MAX_PROPOSALS_GITEA // 50)
        self.assertEqual(forge.list_payloads[0]["authorId"], 7)
        outcomes, _ = harness.last()
        self.assertIsNone(outcomes)
        self.assertEqual(harness.metrics.failures, ["truncated"])

    def test_a_warming_cache_takes_outcomes_and_not_details_until_every_proposal_is_current(self):
        forge = FakeForge()
        for number in range(1, 41):
            forge.add(number, "merged")
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual(outcomes.merged, 40)
        self.assertIsNone(details)
        self.assertEqual(harness.metrics.failures, ["warming"])

        for _ in range(5):
            harness.clock.now += vcs_readings.VCS_READINGS_TTL_SECONDS
            harness.sweep.run_once()
            if harness.last()[1] is not None:
                break
        outcomes, details = harness.last()
        self.assertIsNotNone(details)
        self.assertEqual(details.buckets["merged"][float("inf")], 40)
        self.assertEqual(harness.metrics.failures[-1], "warming")
        self.assertEqual(harness.metrics.failures.count("warming"), len(harness.metrics.readings) - 1)

    def test_the_cache_is_written_per_proposal_and_a_cut_short_attempt_continues(self):
        forge = FakeForge()
        for number in range(1, 6):
            forge.add(number, "open")
        # The listing is newest first; the third proposal read is cut short.
        order = sorted(forge.proposals, key=lambda n: forge.proposals[n]["updated"], reverse=True)
        forge.fail["reviews"] = lambda number: rate_limited() if number == order[2] else None
        harness = Harness({REPO: forge}, interval=WIDE)

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        self.assertEqual(harness.metrics.failures, ["rate_limited"])
        self.assertEqual(len(harness.sweep._cache), 2)
        del forge.fail["reviews"]
        forge.calls.clear()

        harness.sweep.run_once()

        # Only the three not yet read cost calls: reviews, view, commits each.
        self.assertEqual(forge.calls.count("reviews"), 3)
        self.assertEqual(len(harness.sweep._cache), 5)
        self.assertIsNotNone(harness.last()[1])

    def test_a_failing_transport_leaves_the_cache_intact(self):
        forge = FakeForge()
        forge.add(1, "merged")
        forge.add(2, "open")
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        self.assertEqual(len(harness.sweep._cache), 2)
        harness.sweep._transport_for = mock.Mock(side_effect=WorkspaceError("mint failed", status=502))

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        self.assertEqual(len(harness.sweep._cache), 2)
        self.assertEqual(harness.metrics.failures, ["forge"])
        self.assertEqual(len(harness.metrics.readings), 1)

        # A repository no longer managed still takes its records out.
        harness.sweep._managed = lambda: frozenset({OTHER_REPO})
        harness.sweep._registry.forges[OTHER_REPO] = FakeForge()
        harness.sweep._transport_for = lambda forge, repo: FakeTransport(forge)
        harness.sweep.run_once()
        self.assertEqual(harness.sweep._cache, {})

    def test_a_proposal_whose_updated_did_not_move_costs_no_detail_call(self):
        forge = FakeForge()
        forge.add(1, "merged")
        forge.add(2, "open")
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        forge.calls.clear()

        harness.sweep.run_once()

        self.assertEqual(forge.calls, ["list"])
        forge.proposals[2]["updated"] = "2027-01-01T00:00:00Z"
        forge.reviews[2] = [review("approved")]
        forge.calls.clear()

        harness.sweep.run_once()

        self.assertEqual(forge.calls, ["list", "reviews", "view", "commits"])
        self.assertEqual(harness.last()[1].touches, {Touch("review_approved", "human"): 1})

        # Within the walk bound, a proposal absent from the listing was deleted.
        del forge.proposals[1]
        harness.sweep.run_once()
        self.assertNotIn(("github.com", "acme/infra", 1), harness.sweep._cache)
        self.assertEqual(harness.last()[1].buckets["merged"][float("inf")], 0)

    def test_the_hourly_detail_budget_bounds_calls_per_sweep(self):
        forge = FakeForge()
        for number in range(1, 501):
            forge.add(number, "open")
        harness = Harness({REPO: forge})
        per_sweep = vcs_readings.VCS_READINGS_DETAIL_CALLS_PER_HOUR * vcs_readings.VCS_READINGS_TTL_SECONDS / 3600
        per_proposal = 3

        harness.sweep.run_once()
        first = forge.detail_calls()
        self.assertGreater(first, 0)
        self.assertLessEqual(first, per_sweep + per_proposal)

        forge.calls.clear()
        harness.sweep.run_once()
        self.assertEqual(forge.detail_calls(), 0, "no time has passed, so nothing has refilled")

        total = first
        for _ in range(15):
            harness.clock.now += vcs_readings.VCS_READINGS_TTL_SECONDS
            forge.calls.clear()
            harness.sweep.run_once()
            spent = forge.detail_calls()
            self.assertLessEqual(spent, per_sweep + per_proposal)
            total += spent
        # Sixteen sweeps across one hour spend the hour's refill plus the
        # bucket's starting share, and overshoot by at most one proposal.
        self.assertLessEqual(total, vcs_readings.VCS_READINGS_DETAIL_CALLS_PER_HOUR + per_sweep + per_proposal)

    def test_a_nameless_credential_leaves_the_other_repositories_whole(self):
        nameless = FakeForge(login="")
        nameless.add(1, "merged")
        refusing = FakeForge(login=IdentityUnknown("fake"))
        refusing.add(1, "merged")
        named = FakeForge()
        named.add(1, "merged")
        named.add(2, "open")
        harness = Harness(
            {REPO: nameless, OTHER_REPO: named, "fake:github.com/acme/third": refusing}
        )

        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            harness.sweep.run_once()
            harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.merged, outcomes.closed), (1, 1, 0))
        self.assertIsNotNone(details)
        self.assertEqual(harness.metrics.failures, ["identity", "identity"])
        self.assertEqual(nameless.whoami_calls, 1)
        self.assertEqual(refusing.whoami_calls, 1)
        self.assertEqual(nameless.calls, [])
        self.assertEqual(len([line for line in logs.output if "cannot name itself" in line]), 1)

    def test_a_rate_limited_step_two_keeps_the_outcomes_reading_current(self):
        forge = FakeForge()
        forge.add(1, "merged")
        forge.add(2, "closed", closer={"actor": INSTALL_BARE, "bot": True})
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        self.assertEqual(harness.last()[1].closed_by, {"install": 1, "human": 0, "bot": 0})

        forge.proposals[1]["updated"] = "2027-01-01T00:00:00Z"
        forge.add(3, "open")
        forge.fail["view"] = rate_limited()
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.merged, outcomes.closed), (1, 1, 1))
        self.assertIsNone(details)
        self.assertEqual(harness.metrics.failures, ["rate_limited"])

        forge.fail["list"] = rate_limited()
        before = len(harness.metrics.readings)
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()
        self.assertEqual(len(harness.metrics.readings), before, "a rate-limited listing publishes nothing")
        self.assertEqual(harness.metrics.failures[-1], "rate_limited")

    def test_a_closed_proposal_reopened_has_no_closer_and_counts_open(self):
        forge = FakeForge()
        forge.add(1, "closed", closer={"actor": "erin", "bot": False})
        harness = Harness({REPO: forge})
        harness.sweep.run_once()
        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.closed), (0, 1))
        self.assertEqual(details.closed_by["human"], 1)

        forge.proposals[1]["state"] = "open"
        forge.proposals[1]["updated"] = "2027-01-01T00:00:00Z"
        forge.calls.clear()
        harness.sweep.run_once()

        outcomes, details = harness.last()
        self.assertEqual((outcomes.open, outcomes.closed), (1, 0))
        self.assertEqual(details.closed_by, {"install": 0, "human": 0, "bot": 0})
        self.assertNotIn("closer", forge.calls)
        record = harness.sweep._cache[("github.com", "acme/infra", 1)]
        self.assertIsInstance(record, ProposalRecord)
        self.assertEqual(record.closer, "")

    def test_a_proposal_past_the_commit_cap_counts_from_the_cap_with_detail_truncated(self):
        forge = FakeForge()
        forge.add(1, "merged", commits=[commit("dave", "dave@example.com")] * 300)
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        _, details = harness.last()
        self.assertEqual(details.touches, {Touch("commit", "human"): FakeForge.COMMIT_CAP})
        self.assertEqual(harness.metrics.failures, ["detail_truncated"])
        self.assertEqual(forge.calls.count("commits"), 3)

    def test_a_conversation_past_its_bound_counts_what_was_read_with_detail_truncated(self):
        forge = FakeForge()
        forge.add(1, "merged", reviews=[review("approved")], comments=[comment("issue"), comment("issue", author="bob")])
        forge.long_conversations.add(1)
        harness = Harness({REPO: forge})

        harness.sweep.run_once()

        _, details = harness.last()
        self.assertEqual(details.touches, {Touch("review_approved", "human"): 1, Touch("comment", "human"): 2})
        self.assertEqual(details.buckets["merged"][3.0], 1)
        self.assertEqual(harness.metrics.failures, ["detail_truncated"])

    def test_run_once_never_overlaps_itself(self):
        forge = FakeForge()
        forge.add(1, "open")
        forge.gate = threading.Event()
        harness = Harness({REPO: forge})
        first = threading.Thread(target=harness.sweep.run_once)
        first.start()
        self.assertTrue(forge.entered.wait(5))

        harness.sweep.run_once()

        self.assertEqual(len(forge.list_payloads), 1)
        forge.gate.set()
        first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(len(harness.metrics.readings), 1)

        thread = harness.sweep.start()
        try:
            self.assertTrue(thread.daemon)
            self.assertEqual(thread.name, "vcs-readings")
        finally:
            harness.sweep.stop()
            thread.join(5)


GiteaForge = next(cls for cls in providers.AVAILABLE if cls.name == "gitea")
GitHubForge = next(cls for cls in providers.AVAILABLE if cls.name == "github")
GITEA_PAGE_CLAMP = 50


def gitea_pr(number, merged=False, state="closed"):
    return {
        "number": number,
        "state": "open" if state == "open" else "closed",
        "merged": merged,
        "merged_at": "2026-10-04T14:20:45Z" if merged else None,
        "title": f"change {number}",
        "user": {"id": 7, "login": "kube-agents"},
        "head": {"ref": "x", "sha": "a" * 40, "repo": {"full_name": "acme/infra"}},
        "base": {"ref": "main"},
        "created_at": RECENT,
        "updated_at": f"2026-10-04T14:{number // 60:02d}:{number % 60:02d}Z",
        "closed_at": None,
    }


class ClampingGitea:
    """A Gitea API that serves at most `clamp` rows a page, whatever `limit` asks,
    and pages by the clamped size, as a stock `MAX_RESPONSE_ITEMS` does."""

    def __init__(self, nodes, clamp=GITEA_PAGE_CLAMP):
        self.nodes = sorted(nodes, key=lambda n: n["updated_at"], reverse=True)
        self.clamp = clamp
        self.pages: list[int] = []

    def whoami(self):
        return "kube-agents"

    def api(self, method, route, *, params=None, with_headers=False, **_):
        params = params or {}
        if route == "user":
            return {"id": 7, "login": "kube-agents"}
        if route == "repos/acme/infra/pulls":
            size = min(params["limit"], self.clamp)
            page = params["page"]
            self.pages.append(page)
            rows = self.nodes[(page - 1) * size : page * size]
            return rows, {"x-total-count": str(len(self.nodes))}
        prefix, _, number = route.rpartition("/")
        if prefix == "repos/acme/infra/pulls" and number.isdigit():
            return next(node for node in self.nodes if node["number"] == int(number))
        # Step 2: every proposal has no reviews, comments, commits or events.
        if with_headers:
            return [], {}
        return []


class GitHubSearch:
    """GitHub's search route over a fixed set of the install's proposals."""

    def __init__(self, count=3, incomplete=False):
        self.items = [
            {
                "number": n,
                "state": "open",
                "user": {"login": INSTALL, "type": "Bot"},
                "created_at": RECENT,
                "updated_at": f"2026-10-04T14:20:{n:02d}Z",
                "closed_at": None,
                "pull_request": {"merged_at": None},
            }
            for n in range(1, count + 1)
        ]
        self.incomplete = incomplete
        self.routes: list[str] = []

    def whoami(self):
        return INSTALL

    def api(self, method, route, *, params=None, **_):
        self.routes.append(route)
        if route == "search/issues":
            return {"total_count": len(self.items), "incomplete_results": self.incomplete, "items": self.items}
        prefix, _, number = route.rpartition("/")
        if prefix == "repos/acme/infra/pulls" and number.isdigit():
            return {**self.items[int(number) - 1], "head": {}, "base": {}}
        return []


def real_forge_sweep(forge, transport, repo, **kwargs):
    metrics = FakeMetrics()
    sweep = ReadingsSweep(
        registry=FakeRegistry({repo: forge}),
        transport_for=lambda f, r: transport,
        metrics=metrics,
        managed_repositories=lambda: frozenset({repo}),
        clock=kwargs.pop("clock", FakeClock()),
        interval=WIDE,
        **kwargs,
    )
    return sweep, metrics


class ListingCompletenessTest(unittest.TestCase):
    def test_a_clamping_gitea_with_more_than_a_hundred_proposals_is_read_whole(self):
        nodes = [gitea_pr(n, merged=n % 4 == 0) for n in range(1, 121)]
        api = ClampingGitea(nodes)
        forge = GiteaForge("gitea.example.com", "/nonexistent/token", label="gitea")
        sweep, metrics = real_forge_sweep(forge, api, GITEA_REPO)

        sweep.run_once()

        self.assertEqual([1, 2, 3], api.pages)
        outcomes, _ = metrics.readings[-1]
        self.assertEqual((0, 30, 90), (outcomes.open, outcomes.merged, outcomes.closed))
        self.assertEqual(120, len([k for k in sweep._cache if k[:2] == ("gitea.example.com", "acme/infra")]))
        self.assertEqual([], metrics.failures)

    def test_a_clamping_gitea_that_ignores_the_page_ends_the_walk_as_short(self):
        # A server or proxy that ignores `page` serves page 1 again and again;
        # the walk must end, short, rather than ask forever for a total it
        # never reaches.
        nodes = [gitea_pr(n) for n in range(1, 121)]
        api = ClampingGitea(nodes)
        serve = api.api

        def ignoring_page(method, route, *, params=None, **kwargs):
            if route == "repos/acme/infra/pulls":
                params = {**params, "page": 1}
            return serve(method, route, params=params, **kwargs)

        api.api = ignoring_page
        forge = GiteaForge("gitea.example.com", "/nonexistent/token", label="gitea")
        sweep, metrics = real_forge_sweep(forge, api, GITEA_REPO)

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            sweep.run_once()

        # Two pages a walk, the second adding nothing; walked twice.
        self.assertEqual(4, len(api.pages))
        self.assertEqual([], metrics.readings)
        self.assertEqual(["forge"], metrics.failures)
        self.assertEqual({}, sweep._cache)

    def test_a_clamped_walk_stops_at_a_page_ceiling(self):
        # Each page adds one new proposal under a total it never reaches: the
        # proposal bound alone would allow a thousand pages.
        forge = FakeForge(clamp=50)
        for number in range(1, 2001):
            forge.add(number, "merged", updated=f"2026-01-01T00:00:00Z{number:05d}")
        served = []

        def one_new_a_page(method_page):
            served.append(method_page)

        forge.before_page = one_new_a_page
        original = forge.proposal_list_by_author

        def listing(api, repo, payload):
            answer = original(api, repo, payload)
            # Page n repeats page 1's proposals but one, which is new.
            first = original(api, repo, {**payload, "page": 1})["proposals"]
            answer["proposals"] = first[:-1] + answer["proposals"][:1]
            return answer

        forge.proposal_list_by_author = listing
        harness = Harness({REPO: forge}, interval=WIDE)

        harness.sweep.run_once()

        ceiling = -(-vcs_readings.VCS_READINGS_MAX_PROPOSALS_GITEA // 50) + vcs_readings.VCS_READINGS_CLAMPED_PAGE_SLACK
        self.assertLessEqual(len(served), 2 * ceiling)
        self.assertEqual(["truncated"], harness.metrics.failures)

    def test_an_incomplete_page_ends_the_walk_at_once(self):
        search = GitHubSearch(count=150, incomplete=True)
        pages = []
        serve = search.api

        def paged(method, route, *, params=None, **kwargs):
            if route == "search/issues" and params and "page" in params:
                pages.append(params["page"])
                answer = serve(method, route, params=params, **kwargs)
                start = (params["page"] - 1) * 99
                return {**answer, "items": answer["items"][start : start + 99]}
            return serve(method, route, params=params, **kwargs)

        search.api = paged
        forge = GitHubForge(refresh=mock.Mock())
        sweep, metrics = real_forge_sweep(forge, search, GITHUB_REPO)

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            sweep.run_once()

        self.assertEqual([1, 1], pages)
        self.assertEqual(["forge"], metrics.failures)

    def test_a_listing_that_shifts_mid_walk_is_walked_again(self):
        forge = FakeForge()
        for number in range(1, 151):
            forge.add(number, "merged", updated=f"2026-01-01T00:00:00Z{number:05d}")
        shifts = []

        def shift(page):
            # Between pages 1 and 2 of the first walk, the newest proposal on
            # page 2 updates and moves to page 1, which has already been read.
            if page == 2 and not shifts:
                order = sorted(forge.proposals.values(), key=lambda p: p["updated"], reverse=True)
                moved = order[100]
                moved["updated"] = "2027-01-01T00:00:00Z"
                shifts.append(moved["number"])

        forge.before_page = shift
        harness = Harness({REPO: forge}, interval=WIDE)

        harness.sweep.run_once()

        self.assertEqual(1, len(shifts))
        self.assertEqual(5, forge.calls.count("list"), "the short walk of three pages is walked once more, in two")
        outcomes, _ = harness.last()
        self.assertEqual(150, outcomes.merged)
        self.assertEqual([], harness.metrics.failures)

    def test_a_listing_short_on_every_walk_publishes_no_outcomes_reading(self):
        forge = FakeForge()
        for number in range(1, 151):
            forge.add(number, "merged", updated=f"2026-01-01T00:00:00Z{number:05d}")
        bump = iter(range(10_000, 20_000))

        def shift(page):
            if page == 2:
                order = sorted(forge.proposals.values(), key=lambda p: p["updated"], reverse=True)
                order[100]["updated"] = f"2027-01-01T00:00:00Z{next(bump)}"

        forge.before_page = shift
        harness = Harness({REPO: forge}, interval=WIDE)

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            harness.sweep.run_once()

        self.assertEqual([], harness.metrics.readings)
        self.assertEqual(["forge"], harness.metrics.failures)
        self.assertEqual(0, forge.detail_calls())

    def test_an_incomplete_github_search_publishes_no_outcomes_reading(self):
        search = GitHubSearch(incomplete=True)
        forge = GitHubForge(refresh=mock.Mock())
        sweep, metrics = real_forge_sweep(forge, search, GITHUB_REPO)

        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            sweep.run_once()

        self.assertEqual([], metrics.readings)
        self.assertEqual(["forge"], metrics.failures)

        search.incomplete = False
        sweep.run_once()
        outcomes, details = metrics.readings[-1]
        self.assertEqual(3, outcomes.open)
        self.assertIsNotNone(details)
        self.assertEqual(["forge"], metrics.failures)


class SearchPacingTest(unittest.TestCase):
    def test_sixty_github_searches_are_paced_to_the_per_minute_rate(self):
        clock = FakeClock()
        sleeps: list[float] = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock.now += seconds

        stamps: list[float] = []
        search = GitHubSearch()
        original = search.api

        def api(method, route, **kwargs):
            if route == "search/issues":
                stamps.append(clock.now)
            return original(method, route, **kwargs)

        search.api = api
        forge = GitHubForge(refresh=mock.Mock())
        sweep, _ = real_forge_sweep(forge, search, GITHUB_REPO, clock=clock, sleep=sleep)
        bound = sweep._bind(GITHUB_REPO)

        for _ in range(60):
            bound.api("GET", "search/issues", params={})
        for _ in range(30):
            bound.api("GET", "repos/acme/infra/pulls/1/reviews")

        rate = vcs_readings.VCS_READINGS_SEARCH_CALLS_PER_MINUTE
        self.assertEqual(25, rate)
        self.assertTrue(sleeps, "sixty searches in one burst never waited")
        self.assertEqual(60, len(stamps))
        for i in range(len(stamps) - rate):
            self.assertGreaterEqual(stamps[i + rate] - stamps[i], 60, f"more than {rate} searches inside one minute at {i}")
        # The first minute's share goes out at once; only the overflow waits.
        self.assertEqual(stamps[:rate], [stamps[0]] * rate)
        self.assertLessEqual(len(sleeps), 60 - rate)

    def test_a_forge_without_a_search_route_is_never_paced(self):
        sleeps: list[float] = []
        forge = FakeForge()
        harness_clock = FakeClock()
        sweep, _ = real_forge_sweep(forge, FakeTransport(forge), REPO, clock=harness_clock, sleep=sleeps.append)
        bound = sweep._bind(REPO)
        for _ in range(60):
            bound.api("GET", "search/issues")
        self.assertEqual([], sleeps)


class SweepTimerTest(unittest.TestCase):
    def _loop_once(self, harness, run_once):
        waits: list[float] = []

        def wait(timeout):
            waits.append(timeout)
            harness.sweep._stop.set()
            return True

        harness.sweep.run_once = run_once
        harness.sweep._stop.wait = wait
        harness.sweep._loop()
        return waits

    def test_a_failed_attempt_waits_the_same_interval_as_a_successful_one(self):
        forge = FakeForge()
        forge.add(1, "open")
        interval = 123.0
        harness = Harness({REPO: forge}, interval=interval)

        ran = mock.Mock(side_effect=RuntimeError("the attempt fell over"))
        with self.assertLogs(LOGGER_NAME, level="ERROR"):
            failed = self._loop_once(harness, ran)
        self.assertEqual([interval], failed)
        ran.assert_called_once_with()

        harness.sweep._stop.clear()
        forge.fail["list"] = rate_limited()
        attempt = ReadingsSweep.run_once.__get__(harness.sweep)
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            refused = self._loop_once(harness, attempt)
        self.assertEqual([interval], refused)
        self.assertEqual(["rate_limited"], harness.metrics.failures)

        harness.sweep._stop.clear()
        del forge.fail["list"]
        succeeded = self._loop_once(harness, attempt)
        self.assertEqual([interval], succeeded)
        self.assertEqual(1, len(harness.metrics.readings))


class SweepIdentityTest(unittest.TestCase):
    def test_the_sweeps_login_comes_from_the_sweep_transport_not_binding_whoami(self):
        import credential_proxy
        import vcs_broker
        from test_credential_proxy import _SweepExecutor
        from test_providers_http_transport import Opener

        github = next(cls for cls in providers.AVAILABLE if cls.name == "github")
        # Recorded, not raising: `BrokeredCredential.ensure` swallows a
        # refresh's exception, so only the call record can show none was made.
        refresh = mock.Mock()
        forge = github(refresh=refresh)
        executor = _SweepExecutor()
        opener = Opener({"data": {"viewer": {"login": INSTALL}}})
        sweep = ReadingsSweep(
            registry=FakeRegistry({GITHUB_REPO: forge}),
            transport_for=lambda f, r: credential_proxy.sweep_transport_for(executor, f, r, opener=opener),
            metrics=FakeMetrics(),
            managed_repositories=lambda: frozenset({GITHUB_REPO}),
            app_credential=credential_proxy.sweep_app_credential,
        )

        with mock.patch.object(vcs_broker.Binding, "whoami", side_effect=AssertionError("Binding.whoami")):
            sweep._bind(GITHUB_REPO)
            self.assertEqual(INSTALL, sweep._login(GITHUB_REPO))

        (request,) = opener.requests
        self.assertEqual("https://api.github.com/graphql", request.full_url)
        self.assertEqual("Bearer ghs_sweep_1", request.get_header("Authorization"))
        self.assertEqual({"author": "app/kube-agents"}, sweep._author_payload(GITHUB_REPO))
        refresh.assert_not_called()

    def test_an_app_credential_is_searched_as_app_slug_with_or_without_the_marking(self):
        # The credential's kind is the primary signal: GraphQL `viewer` may name
        # an App installation without the `[bot]` marking.
        for login in (INSTALL, INSTALL_BARE):
            with self.subTest(login=login):
                forge = FakeForge(login=login)
                forge.add(1, "open", author=INSTALL_BARE)
                harness = Harness({REPO: forge}, app_credential=lambda f: True)
                harness.sweep.run_once()
                self.assertEqual("app/kube-agents", forge.list_payloads[0]["author"])

        # Not an App: the bare login, unless the marking says otherwise.
        for login, author in ((INSTALL_BARE, INSTALL_BARE), (INSTALL, "app/kube-agents")):
            with self.subTest(login=login, app=False):
                forge = FakeForge(login=login)
                forge.add(1, "open", author=INSTALL_BARE)
                harness = Harness({REPO: forge}, app_credential=lambda f: False)
                harness.sweep.run_once()
                self.assertEqual(author, forge.list_payloads[0]["author"])


class PureFunctionTest(unittest.TestCase):
    def test_bucket_counts_are_cumulative_with_inf_last(self):
        counts = bucket_counts([0, 0, 1, 4, 30])
        self.assertEqual(list(counts), list(vcs_readings.VCS_READINGS_BUCKETS))
        self.assertEqual(counts[0.0], 2)
        self.assertEqual(counts[3.0], 3)
        self.assertEqual(counts[5.0], 4)
        self.assertEqual(counts[21.0], 4)
        self.assertEqual(counts[math.inf], 5)




class ManagedKeyTest(unittest.TestCase):
    """The managed list keys repositories as `provider:host/path`; the registry
    takes `host/path`. The sweep's first live run on a GitHub install failed
    every attempt with `ForgeUnsupported` because the key went to the registry
    whole, and the provider read as a host no forge serves."""

    def test_a_managed_key_is_resolved_by_its_host_and_path(self):
        harness = Harness({REPO: FakeForge()}, interval=WIDE)
        harness.sweep.run_once()
        outcomes, _ = harness.last()
        self.assertIsNotNone(outcomes)
        self.assertEqual([], harness.metrics.failures)

    def test_a_key_registered_as_another_provider_admits_nothing(self):
        # The host resolves to the fake forge, whose provider is `fake`, and
        # the entry says `github`: refused with the failure counted, no reading.
        forge = FakeForge()
        forge.add(1, "merged")
        harness = Harness({"github:github.com/acme/infra": forge}, interval=WIDE)
        harness.sweep.run_once()
        self.assertEqual(["forge"], harness.metrics.failures)
        self.assertEqual([], harness.metrics.readings)
        self.assertEqual([], forge.calls)


class TallyAndUnsupportedTest(unittest.TestCase):
    def _truncated_forge(self, tally):
        forge = FakeForge(host="gitea.example.com", login="kube-agents", clamp=50, splits=True, http_user=True)
        for number in range(1, 1102):
            forge.add(number, "merged", updated=f"2026-01-01T00:00:00Z{number:05d}")
        forge.proposal_tally = tally
        return forge

    def test_a_failed_tally_withholds_the_outcomes_reading_only(self):
        def tally(api, repo, payload):
            raise WorkspaceError("search down", status=502, code="FORGE_CALL_FAILED")

        forge = self._truncated_forge(tally)
        harness = Harness({GITEA_REPO.replace("gitea:", "fake:"): forge}, interval=WIDE)
        harness.sweep.run_once()
        outcomes, details = harness.last()
        self.assertIsNone(outcomes)
        self.assertIsNotNone(details, "the details reading is published from the listing the tally did not touch")
        self.assertEqual(["truncated", "forge"], harness.metrics.failures)

    def test_a_failed_tally_keeps_its_reason_beside_warming(self):
        # Under the default interval the budget warms about nine proposals a
        # sweep, so a repository past the bound warms for many sweeps; the
        # tally's failure stays the reason the outcomes reading is withheld.
        def tally(api, repo, payload):
            raise WorkspaceError("search down", status=502, code="FORGE_CALL_FAILED")

        forge = self._truncated_forge(tally)
        harness = Harness({GITEA_REPO.replace("gitea:", "fake:"): forge})
        harness.sweep.run_once()
        self.assertEqual(["truncated", "forge", "warming"], harness.metrics.failures)
        self.assertEqual([], harness.metrics.readings)

    def test_a_forge_that_does_not_serve_the_sweep_withholds_the_readings_each_sweep(self):
        # The design's rule: a repository the forge cannot serve is a failed
        # required repository, so no sum is published without it, every sweep.
        served = FakeForge()
        served.add(1, "merged")
        refusing = FakeForge()

        def refuse(api, repo, payload):
            raise vcs_readings.ForgeUnsupported("fake does not serve `proposal-list-by-author`")

        refusing.proposal_list_by_author = refuse
        harness = Harness({REPO: served, OTHER_REPO: refusing}, interval=WIDE)
        harness.sweep.run_once()
        self.assertEqual([], harness.metrics.readings)
        self.assertEqual(["forge"], harness.metrics.failures)
        harness.sweep.run_once()
        self.assertEqual(["forge", "forge"], harness.metrics.failures, "attempted again every sweep")
        self.assertEqual([], harness.metrics.readings)


class MalformedListingTest(unittest.TestCase):
    def test_a_listed_proposal_with_no_number_fails_that_repository_and_not_the_sweep(self):
        # One such item used to raise out of `run_once` before any failure was
        # counted; it is a forge failure of that repository, and the other
        # repositories are still attempted.
        broken = FakeForge()
        broken.add(1, "merged")
        listed = broken.proposal_list_by_author

        def nulled(api, repo, payload):
            answer = listed(api, repo, payload)
            for item in answer["proposals"]:
                item["number"] = None
            return answer

        broken.proposal_list_by_author = nulled
        other = FakeForge()
        other.add(2, "merged")
        harness = Harness({REPO: broken, OTHER_REPO: other}, interval=WIDE)
        harness.sweep.run_once()
        self.assertEqual(["forge"], harness.metrics.failures)
        self.assertEqual([], harness.metrics.readings, "all or nothing: the sum is withheld")
        self.assertIn("list", other.calls, "the other repository was still attempted")


class TallyWithOpenTest(unittest.TestCase):
    def test_open_is_the_remainder_of_the_one_count(self):
        self.assertEqual({"merged": 5, "closed": 1, "total": 8, "open": 2}, providers.tally_with_open({"merged": 5, "closed": 1, "total": 8}))

    def test_a_count_that_straddled_a_merge_is_clamped_at_zero(self):
        # merged and closed each counted a proposal that merged between the
        # requests; the remainder would be -1 and a gauge never goes negative.
        self.assertEqual(0, providers.tally_with_open({"merged": 2, "closed": 1, "total": 2})["open"])


class GitHubTallyTest(unittest.TestCase):
    def test_the_tally_adds_the_unqualified_count_as_total(self):
        forge = GitHubForge(refresh=mock.Mock())
        queries = []

        def api(method, path, params=None, **kwargs):
            queries.append(params["q"])
            counts = {"is:open": 2, "is:merged": 5, "is:unmerged is:closed": 1}
            total = next((v for k, v in counts.items() if params["q"].endswith(" " + k)), 8)
            return {"total_count": total, "incomplete_results": False, "items": []}

        answer = forge.proposal_tally(api, "acme/infra", {"author": "app/kube-agents"})
        self.assertEqual({"open": 2, "merged": 5, "closed": 1, "total": 8}, answer)
        self.assertEqual(3, len(queries), "merged, closed and the one count; open is derived, not searched")
        self.assertFalse(queries[-1].endswith(" "), "the unqualified query carries no trailing space")


if __name__ == "__main__":
    unittest.main()
