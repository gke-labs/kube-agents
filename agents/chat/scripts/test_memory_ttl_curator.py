"""Unit tests for the memory TTL curator's selection and safety logic.

Run: python3 -m unittest agents/chat/scripts/test_memory_ttl_curator.py

This script deletes memory unattended, so the tests lean on the refusal paths:
what survives matters more than what goes. Every guard that stops a run —
too-small bank, missing retain strategy, no observations, unscopable
observation, partial distil — is exercised, and the happy path asserts exactly
which rows were retired and why. Hindsight is a fake; what is tested is the
decision logic, not HTTP.
"""

import contextlib
import io
import json
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import memory_ttl_curator as curator  # noqa: E402

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
TTL_DAYS = 180
BANK = "kube-agents-memory"


def days_ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def fact(uid, *, age_days, fact_type="world", context=None, tags=None):
    unit = {
        "id": uid,
        "type": fact_type,
        "state": "valid",
        "mentioned_at": days_ago(age_days),
        "tags": tags or ["user:alice"],
    }
    if context is not None:
        unit["context"] = context
    return unit


def observation(uid, *, text="the fleet runs three clusters", tags=None):
    return {
        "id": uid,
        "type": "observation",
        "text": text,
        "tags": ["user:alice"] if tags is None else tags,
    }


class FakeHindsight:
    """Answers the five calls curate() makes, from an in-memory unit list.

    `fail_retains` names 0-based retain-call indices that raise, which is how a
    test stages a partial distil.
    """

    def __init__(self, units, strategies=(curator.CHECKPOINT_STRATEGY,),
                 fail_retains=()):
        self._units = list(units)
        self._strategies = strategies
        self._fail = set(fail_retains)
        self.retain_calls = []
        self.invalidated = []
        self.consolidations = 0

    def count(self, bank_id):
        return len(self._units)

    def bank_config(self, bank_id):
        return {"retain_strategies": {s: {} for s in self._strategies}}

    def units(self, bank_id, **filters):
        found = []
        for unit in self._units:
            if "type" in filters and unit.get("type") != filters["type"]:
                continue
            if "state" in filters and unit.get("state", "valid") != filters["state"]:
                continue
            found.append(unit)
        return found

    def retain(self, bank_id, items):
        index = len(self.retain_calls)
        self.retain_calls.append(items)
        if index in self._fail:
            raise RuntimeError("HTTP 500 on POST /memories: extraction failed")
        # `chunks` mode, one short item in -> one row out.
        for i, item in enumerate(items):
            self._units.append({
                "id": f"cp-new-{index}-{i}",
                "type": "world",
                "state": "valid",
                "context": item["context"],
                "mentioned_at": NOW.isoformat(),
                "tags": item["tags"],
            })
        return {}

    def invalidate(self, bank_id, memory_id, reason):
        self.invalidated.append((memory_id, reason))
        return {}

    def consolidate(self, bank_id):
        self.consolidations += 1
        return {}


def run(api, *, commit=True, ttl_days=TTL_DAYS, min_units=1):
    return curator.curate(api, BANK, ttl_days=ttl_days, min_units=min_units,
                          commit=commit, now=NOW)


# --------------------------------------------------------------------------- #
# The guards that stop a run before it writes anything
# --------------------------------------------------------------------------- #


class TestGuards(unittest.TestCase):
    def test_a_small_bank_is_left_alone(self):
        api = FakeHindsight([fact("f1", age_days=400)])
        summary = run(api, min_units=200)
        self.assertIn("< min 200", summary["skipped"])
        self.assertEqual(api.retain_calls, [])
        self.assertEqual(api.invalidated, [])

    def test_a_bank_without_the_checkpoint_strategy_is_refused(self):
        # Without it Hindsight would paraphrase the checkpoints, and the retire
        # pass would then trust the paraphrase.
        api = FakeHindsight([fact("f1", age_days=400)], strategies=("concise",))
        summary = run(api)
        self.assertIn(repr(curator.CHECKPOINT_STRATEGY), summary["skipped"])
        self.assertEqual(api.retain_calls, [])
        self.assertEqual(api.invalidated, [])

    def test_nothing_older_than_the_ttl_means_nothing_happens(self):
        api = FakeHindsight([
            fact("f1", age_days=10),
            observation("o1"),
        ])
        summary = run(api)
        self.assertEqual(summary["skipped"], f"nothing older than {TTL_DAYS}d")
        self.assertEqual(api.invalidated, [])

    def test_aged_facts_with_no_observations_are_kept_not_orphaned(self):
        # Retiring here would delete the evidence with nothing distilled from
        # it. Both consolidation-never-ran and consolidation-broken look like
        # this, and both want a human.
        api = FakeHindsight([fact("f1", age_days=400)])
        summary = run(api)
        self.assertIn("no observations to distil", summary["skipped"])
        self.assertEqual(api.invalidated, [])

    def test_an_unscopable_observation_aborts_the_whole_run(self):
        # One observation with no scope tag is knowledge that would be retired
        # without a home. The run stops before writing anything at all.
        api = FakeHindsight([
            fact("f1", age_days=400),
            observation("o1"),
            observation("o2", tags=["session:s1"]),  # no scope tag
        ])
        summary = run(api)
        self.assertIn("cannot be scoped", summary["skipped"])
        self.assertEqual(api.retain_calls, [])
        self.assertEqual(api.invalidated, [])


# --------------------------------------------------------------------------- #
# Age selection — the boundary, and rows that cannot be dated
# --------------------------------------------------------------------------- #


class TestAgeSelection(unittest.TestCase):
    def test_the_cutoff_is_exclusive_a_fact_exactly_at_ttl_survives(self):
        api = FakeHindsight([
            fact("boundary", age_days=TTL_DAYS),      # anchor == cutoff
            fact("older", age_days=TTL_DAYS + 1),
            observation("o1"),
        ])
        run(api)
        retired = [uid for uid, _ in api.invalidated]
        self.assertIn("older", retired)
        self.assertNotIn("boundary", retired)

    def test_a_row_with_an_unparseable_date_is_skipped_not_deleted(self):
        # A malformed timestamp means the age is unknown, and unknown must not
        # be treated as old.
        mangled = fact("mangled", age_days=400)
        mangled["mentioned_at"] = "not-a-date"
        mangled.pop("date", None)
        api = FakeHindsight([
            mangled,
            fact("older", age_days=400),
            observation("o1"),
        ])
        run(api)
        retired = [uid for uid, _ in api.invalidated]
        self.assertIn("older", retired)
        self.assertNotIn("mangled", retired)

    def test_a_future_dated_row_is_not_swept(self):
        # The `anchor < now` clause: a --ttl-days 0 run must not consume rows
        # stamped after the run started.
        future = fact("future", age_days=0)
        future["mentioned_at"] = (NOW + timedelta(hours=1)).isoformat()
        api = FakeHindsight([
            future,
            fact("older", age_days=1),
            observation("o1"),
        ])
        run(api, ttl_days=0)
        retired = [uid for uid, _ in api.invalidated]
        self.assertIn("older", retired)
        self.assertNotIn("future", retired)

    def test_ingestion_date_wins_over_the_event_date(self):
        # A fact about something years past, learned yesterday, is not stale.
        recent_about_old = fact("recent", age_days=1)
        recent_about_old["date"] = days_ago(1000)
        api = FakeHindsight([
            recent_about_old,
            fact("older", age_days=400),
            observation("o1"),
        ])
        run(api)
        retired = [uid for uid, _ in api.invalidated]
        self.assertNotIn("recent", retired)


# --------------------------------------------------------------------------- #
# The commit path — distil, verify, then retire
# --------------------------------------------------------------------------- #


class TestCommit(unittest.TestCase):
    def make_api(self, **kwargs):
        return FakeHindsight([
            fact("aged", age_days=400),
            fact("fresh", age_days=10),
            fact("oldcp", age_days=7, context=curator.CHECKPOINT_CONTEXT),
            observation("o1", tags=["user:alice", "session:s1"]),
            observation("o2", tags=["scope:shared"]),
        ], **kwargs)

    def test_a_dry_run_reports_the_plan_and_writes_nothing(self):
        api = self.make_api()
        summary = run(api, commit=False)
        self.assertEqual(summary["skipped"], "dry run")
        self.assertEqual(summary["distilled"], 2)
        self.assertEqual(summary["retired"], 2)  # aged + oldcp
        self.assertEqual(api.retain_calls, [])
        self.assertEqual(api.invalidated, [])
        self.assertEqual(api.consolidations, 0)

    def test_the_happy_path_retires_exactly_the_aged_and_the_superseded(self):
        api = self.make_api()
        summary = run(api)
        self.assertIsNone(summary["skipped"])
        self.assertEqual(summary["distilled"], 2)
        self.assertEqual(summary["retired"], 2)

        retired = dict(api.invalidated)
        self.assertIn("aged", retired)
        self.assertIn("oldcp", retired)
        self.assertNotIn("fresh", retired)
        # The reason distinguishes the TTL sweep from checkpoint supersession,
        # because an operator un-retiring a row needs to know which it was.
        self.assertIn("retained more than 180d ago", retired["aged"])
        self.assertIn("superseded by a fresher checkpoint", retired["oldcp"])
        self.assertEqual(api.consolidations, 1)

    def test_checkpoints_are_written_one_per_call(self):
        # Retain is neither atomic nor idempotent, so batching is how a single
        # bad item once tripled a bank. One item per call is load-bearing.
        api = self.make_api()
        run(api)
        self.assertEqual(len(api.retain_calls), 2)
        for items in api.retain_calls:
            self.assertEqual(len(items), 1)

    def test_checkpoints_carry_the_scope_and_the_marker_context(self):
        api = self.make_api()
        run(api)
        items = [items[0] for items in api.retain_calls]
        by_scope = {item["observation_scopes"][0][0]: item for item in items}
        self.assertEqual(set(by_scope), {"user:alice", "scope:shared"})
        alice = by_scope["user:alice"]
        # Every tag rides along so topical filters keep working; the scope is
        # pinned separately so session tags cannot fragment consolidation.
        self.assertEqual(alice["tags"], ["user:alice", "session:s1"])
        self.assertEqual(alice["context"], curator.CHECKPOINT_CONTEXT)
        self.assertEqual(alice["strategy"], curator.CHECKPOINT_STRATEGY)

    def test_a_partial_distil_retires_nothing(self):
        # The one way this script can lose knowledge is a full retire after a
        # partial distil. A checkpoint that fails to land must stop the run.
        api = self.make_api(fail_retains=(1,))
        with contextlib.redirect_stderr(io.StringIO()):
            summary = run(api)
        self.assertIn("aborted before retiring", summary["skipped"])
        self.assertIn("1/2 checkpoints landed", summary["skipped"])
        self.assertEqual(api.invalidated, [])
        self.assertEqual(api.consolidations, 0)

    def test_an_observation_with_empty_text_is_dropped_without_aborting(self):
        api = FakeHindsight([
            fact("aged", age_days=400),
            observation("blank", text="   "),
            observation("o1"),
        ])
        summary = run(api)
        self.assertIsNone(summary["skipped"])
        self.assertEqual(len(api.retain_calls), 1)
        self.assertEqual(summary["distilled"], 1)


# --------------------------------------------------------------------------- #
# The HTTP client's retry policy
# --------------------------------------------------------------------------- #


class _Http:
    """A scripted urlopen: each entry is an int status to raise or a dict to return."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, int):
            raise urllib.error.HTTPError(
                request.full_url, step, "boom", {}, io.BytesIO(b"detail"))
        return io.BytesIO(json.dumps(step).encode())


class TestHindsightClient(unittest.TestCase):
    def api(self, script):
        http = _Http(script)
        patcher = patch("urllib.request.urlopen", http)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The backoff is real time.sleep; a test must not pay it.
        sleeper = patch.object(curator.time, "sleep", lambda s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        return curator.Hindsight("http://kv.example"), http

    def test_a_rate_limit_is_retried_until_it_clears(self):
        api, http = self.api([429, 429, {"ok": True}])
        self.assertEqual(api.call("GET", "/x"), {"ok": True})
        self.assertEqual(http.calls, 3)

    def test_a_non_retryable_status_raises_immediately(self):
        api, http = self.api([404])
        with self.assertRaises(RuntimeError) as caught:
            api.call("GET", "/x")
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertEqual(http.calls, 1)

    def test_retain_does_not_retry_a_500(self):
        # Retain persists its successful prefix before failing, so a retried
        # 500 duplicates rows — it once turned 646 units into 1,959. The 500
        # must surface on the first attempt.
        api, http = self.api([500])
        with self.assertRaises(RuntimeError):
            api.retain("bank", [{"content": "x"}])
        self.assertEqual(http.calls, 1)

    def test_everything_else_still_retries_a_500(self):
        api, http = self.api([500, {"config": {}}])
        self.assertEqual(api.bank_config("bank"), {})
        self.assertEqual(http.calls, 2)


# --------------------------------------------------------------------------- #
# The helpers the selection rests on
# --------------------------------------------------------------------------- #


class TestHelpers(unittest.TestCase):
    def test_parse_time_reads_zulu_and_naive_timestamps(self):
        self.assertEqual(
            curator.parse_time("2026-08-19T12:00:00Z"),
            datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc),
        )
        naive = curator.parse_time("2026-08-19T12:00:00")
        self.assertEqual(naive.tzinfo, timezone.utc)

    def test_parse_time_returns_none_for_garbage_not_a_default(self):
        for value in (None, "", "not-a-date", "2026-13-45T99:99:99Z"):
            with self.subTest(value=value):
                self.assertIsNone(curator.parse_time(value))

    def test_scope_tags_keeps_scopes_and_drops_provenance(self):
        unit = {"tags": ["user:alice", "session:s1", "scope:shared", "topic:gke"]}
        self.assertEqual(curator.scope_tags(unit), ["user:alice", "scope:shared"])
        self.assertEqual(curator.scope_tags({}), [])

    def test_build_checkpoints_reports_every_unscopable_observation(self):
        items, problems = curator.build_checkpoints([
            observation("good"),
            observation("untagged", tags=[]),
            observation("torn", tags=["user:a", "user:b"]),
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(len(problems), 2)
        self.assertIn("no scope tag", problems[0])
        self.assertIn("2 scope tags", problems[1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
