#!/usr/bin/env python3
"""Tests for merge_gate.py -- the merge queue's gate checks and the enqueue sweep.

Run: cd scripts && python3 -m unittest test_merge_gate

The two wrong answers cost differently. A gate that passes when Tide would
not have merged lets in a pull request that is unapproved or unsmoked; a gate
that holds when Tide would have merged leaves a ready pull request out of the queue
with nothing on it saying why. So each verdict gets both sides, the smoke
skip list is checked against the paths Prow skips, and the queue branch is
parsed from the shape GitHub names it.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import merge_gate as gate
import pin_smoke_status

REPO = "gke-labs/kube-agents"
SHA = "a" * 40
BASE_SHA = "b" * 40
APPROVED = {"lgtm", "approved", "size/M"}
CUTOFF = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
BUILD = "https://prow.example/view/gs/bucket/pr-logs/pull/7/pull-kube-agents-smoke-test/1"


def pull(labels=APPROVED, statuses=None, draft=False, auto_merge=False, queue_entry=None, base="main"):
    return {
        "id": "PR_1",
        "number": 7,
        "sha": SHA,
        "base": base,
        "draft": draft,
        "auto_merge": auto_merge,
        "queue_entry": queue_entry,
        "labels": set(labels),
        "statuses": statuses or {},
    }


def green(*contexts):
    return {context: ("success", "") for context in contexts}


def no_files():
    raise AssertionError("the file list was read when it was not needed")


class FakeAPI:
    """Just enough of `GitHubAPI` for the functions that call it."""

    def __init__(self, raw_pull=None, statuses=(), files=(), nodes=(), all_statuses=(), comments=()):
        self.repo = REPO
        self.raw_pull = raw_pull
        self.statuses = list(statuses)
        self.all_statuses = list(all_statuses)
        self.files = list(files)
        self.nodes = list(nodes)
        self.comments = list(comments)
        self.comment_reads = []
        self.refuse_comments = False
        self.refuse_comment_reads = False
        self.mutations = []
        self.queries = []
        self.posts = []

    def get(self, path, tolerate=()):
        if path.endswith("/status?per_page=100"):
            return {"statuses": self.statuses}
        return self.raw_pull

    def get_all(self, path, per_page=100):
        if path.endswith("/files"):
            return self.files
        if path.endswith("/statuses"):
            return self.all_statuses
        if "/comments" in path:
            if self.refuse_comment_reads:
                raise RuntimeError("HTTP 502")
            self.comment_reads.append(path)
            return self.comments
        return [self.raw_pull]

    def post(self, path, payload=None, tolerate=()):
        if self.refuse_comments and path.endswith("/comments"):
            raise RuntimeError("HTTP 403")
        self.posts.append((path, payload))

    def graphql(self, query, variables):
        self.queries.append(query)
        if query.startswith("mutation"):
            self.mutations.append((query.split("(", 2)[1].split("{")[-1].strip(), variables["id"]))
            return {}
        return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.nodes}}}


class LabelsTest(unittest.TestCase):
    def test_both_labels_and_no_hold_pass(self):
        self.assertEqual(gate.labels_verdict(APPROVED)[0], gate.SUCCESS)

    def test_a_missing_label_waits_and_names_it(self):
        state, description = gate.labels_verdict({"approved"})
        self.assertEqual(state, gate.PENDING)
        self.assertIn("lgtm", description)

    def test_each_label_tide_excluded_holds(self):
        for hold in ("do-not-merge", "do-not-merge/hold", "do-not-merge/invalid-owners-file", "do-not-merge/work-in-progress"):
            with self.subTest(label=hold):
                state, description = gate.labels_verdict(APPROVED | {hold})
                self.assertEqual(state, gate.PENDING)
                self.assertIn(hold, description)

    def test_a_do_not_merge_label_tide_did_not_exclude_does_not_hold(self):
        """Holding on it would keep a pull request out of the queue that Tide would have merged."""
        for label in ("do-not-merge/release-note", "do-not-merge-ish"):
            with self.subTest(label=label):
                self.assertEqual(gate.labels_verdict(APPROVED | {label})[0], gate.SUCCESS)


class SmokeTest(unittest.TestCase):
    def test_a_green_smoke_passes_without_reading_files(self):
        results = gate.verdicts(pull(statuses=green(gate.SMOKE_CONTEXT)), no_files)
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.SUCCESS)

    def test_a_red_smoke_fails(self):
        statuses = {gate.SMOKE_CONTEXT: ("failure", "Job failed.")}
        results = gate.verdicts(pull(statuses=statuses), lambda: ["a2a/main.go"])
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.FAILURE)

    def test_a_running_smoke_waits(self):
        statuses = {gate.SMOKE_CONTEXT: ("pending", "Job triggered.")}
        results = gate.verdicts(pull(statuses=statuses), lambda: ["a2a/main.go"])
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.PENDING)

    def test_no_smoke_on_a_change_it_runs_for_waits(self):
        results = gate.verdicts(pull(), lambda: ["docs/a.md", "agents/chat/SOUL.md"])
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.PENDING)

    def test_no_smoke_on_skipped_paths_passes(self):
        """Prow posts no smoke status on these, and Tide did not wait for one."""
        files = ["docs/a.md", ".github/workflows/x.yml", "README.md", "scripts/test_x.py", "OWNERS", "install.sh"]
        results = gate.verdicts(pull(), lambda: files)
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.SUCCESS)

    def test_a_red_or_running_smoke_on_skipped_paths_still_decides(self):
        """A hand-run `/test` posts a smoke status on a skip-only change; Tide held on its red."""
        for state, expected in (("failure", gate.FAILURE), ("pending", gate.PENDING)):
            with self.subTest(state=state):
                statuses = {gate.SMOKE_CONTEXT: (state, "")}
                results = gate.verdicts(pull(statuses=statuses), lambda: ["docs/a.md"])
                self.assertEqual(results[gate.SMOKE_GATE][0], expected)

    def test_the_skip_list_matches_prows_reading(self):
        for path in ("tests/x.py", "examples/a/b.yaml", "scripts/release/x.sh", "scripts/eval_dashboard/x.py", "LICENSE"):
            with self.subTest(path=path):
                self.assertFalse(gate.smoke_required([path]))
        for path in ("scripts/x.py", "scripts/sub/test_x.py", "docs.md/x", "agents/a.md", "install.sh.bak"):
            with self.subTest(path=path):
                self.assertTrue(gate.smoke_required([path]))

    def test_an_overlong_file_list_needs_the_smoke_and_an_empty_one_does_not(self):
        """Prow skips a `skip_if_only_changed` job when no file changed."""
        self.assertTrue(gate.smoke_required(["docs/a.md"] * gate.FILES_CEILING))
        self.assertFalse(gate.smoke_required([]))

    def test_a_rename_counts_by_its_new_path_as_prow_reads_it(self):
        """Prow posts no smoke status for a move into a skipped path, so the gate must not wait for one."""
        api = FakeAPI(files=[{"filename": "docs/x.py", "previous_filename": "scripts/x.py"}])
        self.assertEqual(gate.changed_files(api, 7), ["docs/x.py"])
        self.assertFalse(gate.smoke_required(gate.changed_files(api, 7)))


def smoke_status(created_at, state="success", target_url=BUILD):
    """One entry of the commit's full status list, which GitHub returns newest first."""
    return {"context": gate.SMOKE_CONTEXT, "state": state, "target_url": target_url, "created_at": created_at}


class CutoffTest(unittest.TestCase):
    """SMOKE_PIN_NOT_BEFORE: a smoke green from before the mode change must not pass."""

    def test_unset_a_green_passes_without_reading_its_history(self):
        def unread():
            raise AssertionError("the status history was read with no cutoff set")

        results = gate.verdicts(pull(statuses=green(gate.SMOKE_CONTEXT)), no_files, None, unread)
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.SUCCESS)

    def test_a_green_from_before_the_cutoff_waits_for_a_retest(self):
        before = datetime(2026, 10, 23, 17, 59, tzinfo=timezone.utc)
        results = gate.verdicts(pull(statuses=green(gate.SMOKE_CONTEXT)), no_files, CUTOFF, lambda: before)
        self.assertEqual(results[gate.SMOKE_GATE], (gate.PENDING, gate.SMOKE_PREDATES))
        self.assertLessEqual(len(gate.SMOKE_PREDATES), gate.DESCRIPTION_MAX)

    def test_a_green_from_after_the_cutoff_passes(self):
        after = datetime(2026, 10, 23, 18, 1, tzinfo=timezone.utc)
        results = gate.verdicts(pull(statuses=green(gate.SMOKE_CONTEXT)), no_files, CUTOFF, lambda: after)
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.SUCCESS)

    def test_a_green_of_unknown_age_waits(self):
        results = gate.verdicts(pull(statuses=green(gate.SMOKE_CONTEXT)), no_files, CUTOFF, lambda: None)
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.PENDING)

    def test_a_red_or_running_smoke_and_skipped_paths_are_unchanged(self):
        def unread():
            raise AssertionError("the status history was read for a smoke that is not green")

        red = {gate.SMOKE_CONTEXT: ("failure", "Job failed.")}
        self.assertEqual(gate.verdicts(pull(statuses=red), no_files, CUTOFF, unread)[gate.SMOKE_GATE][0], gate.FAILURE)
        skipped = gate.verdicts(pull(), lambda: ["docs/a.md"], CUTOFF, unread)
        self.assertEqual(skipped[gate.SMOKE_GATE][0], gate.SUCCESS)

    def test_a_re_pin_copy_counts_from_the_builds_first_report(self):
        """A re-pin after the cutoff is a fresh copy of a green from before it."""
        api = FakeAPI(all_statuses=[smoke_status("2026-10-23T19:00:00Z"), smoke_status("2026-10-23T12:00:00Z")])
        self.assertEqual(gate.smoke_first_reported(api, SHA), datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc))

    def test_an_other_build_or_a_red_in_the_history_is_not_its_first_report(self):
        api = FakeAPI(
            all_statuses=[
                smoke_status("2026-10-23T19:00:00Z"),
                smoke_status("2026-10-23T12:00:00Z", state="pending"),
                smoke_status("2026-10-23T11:00:00Z", target_url=BUILD.replace("/1", "/0")),
            ]
        )
        self.assertEqual(gate.smoke_first_reported(api, SHA), datetime(2026, 10, 23, 19, 0, tzinfo=timezone.utc))

    def test_a_status_with_no_build_link_counts_from_the_earliest_with_none(self):
        """A re-pin copies a link-less green with a new time; the copy must not pass for a new verdict."""
        api = FakeAPI(
            all_statuses=[
                smoke_status("2026-10-23T19:00:00Z", target_url=""),
                smoke_status("2026-10-23T12:00:00Z", target_url=None),
                smoke_status("2026-10-23T11:00:00Z"),
            ]
        )
        self.assertEqual(gate.smoke_first_reported(api, SHA), datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc))

    def test_an_unreadable_time_or_a_latest_that_is_not_green_is_unknown(self):
        for statuses in (
            [smoke_status("")],
            [smoke_status("2026-10-23T19:00:00")],
            [smoke_status("2026-10-23T19:00:00Z", state="pending")],
            [],
        ):
            with self.subTest(statuses=statuses):
                self.assertIsNone(gate.smoke_first_reported(FakeAPI(all_statuses=statuses), SHA))

    def test_the_variable_is_read_as_pin_smoke_status_reads_it(self):
        for raw, want in (("", None), ("  ", None), ("2026-10-23T18:00:00Z", CUTOFF), ("2026-10-23T14:00:00-04:00", CUTOFF)):
            with self.subTest(raw=raw):
                self.assertEqual(gate.not_before_from_env({gate.NOT_BEFORE_ENV: raw}), want)
        self.assertIsNone(gate.not_before_from_env({}))
        for raw in ("2026-10-23T18:00:00", "tomorrow"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                gate.not_before_from_env({gate.NOT_BEFORE_ENV: raw})

    def test_an_unparsable_variable_decides_nothing(self):
        env = {"GITHUB_TOKEN": "t", gate.NOT_BEFORE_ENV: "tomorrow"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gate, "cmd_group") as group:
            self.assertEqual(gate.main(["--repo", REPO, "group", "--ref", "x", "--gate", gate.SMOKE_GATE]), 2)
        group.assert_not_called()

    def test_a_merge_labels_group_does_not_read_the_variable(self):
        env = {"GITHUB_TOKEN": "t", gate.NOT_BEFORE_ENV: "tomorrow"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gate, "cmd_group") as group:
            group.return_value = True, "#42 Merge labels: success"
            self.assertEqual(gate.main(["--repo", REPO, "group", "--ref", "x", "--gate", gate.LABELS_GATE]), 0)
        self.assertIsNone(group.call_args.kwargs["not_before"])

    def test_a_green_pin_smoke_status_expired_waits_with_or_without_the_variable(self):
        def unread():
            raise AssertionError("the status history was read for an expired green")

        expired = {gate.SMOKE_CONTEXT: ("success", f"Job succeeded. {gate.SMOKE_EXPIRED_NOTE}")}
        for not_before in (None, CUTOFF):
            with self.subTest(not_before=not_before):
                results = gate.verdicts(pull(statuses=expired), no_files, not_before, unread)
                self.assertEqual(results[gate.SMOKE_GATE], (gate.PENDING, gate.SMOKE_PREDATES))

    def test_the_expired_note_is_the_one_pin_smoke_status_writes(self):
        self.assertEqual(gate.SMOKE_EXPIRED_NOTE, pin_smoke_status.EXPIRED_NOTE)
        self.assertIn(gate.SMOKE_EXPIRED_NOTE, pin_smoke_status.expired_description("x" * 200))

    def test_the_queue_refuses_a_green_from_before_the_cutoff(self):
        raw = {"number": 42, "head": {"sha": SHA}, "base": {"ref": "main"}, "labels": []}
        ref = f"gh-readonly-queue/main/pr-42-{BASE_SHA}"
        for created, passed in (("2026-10-23T17:00:00Z", False), ("2026-10-23T19:00:00Z", True)):
            with self.subTest(created=created):
                head = [{"context": gate.SMOKE_CONTEXT, "state": "success"}]
                api = FakeAPI(raw_pull=raw, statuses=head, all_statuses=[smoke_status(created)])
                self.assertEqual(gate.cmd_group(api, ref, gate.SMOKE_GATE, not_before=CUTOFF)[0], passed)

    def test_a_head_status_is_not_posted_green_for_a_green_from_before_the_cutoff(self):
        api = FakeAPI(all_statuses=[smoke_status("2026-10-23T17:00:00Z")])
        gate.gate_pull(api, pull(statuses=green(gate.SMOKE_CONTEXT)), not_before=CUTOFF)
        self.assertEqual([payload["context"] for _, payload in api.posts], [gate.LABELS_GATE])

    def test_a_head_smoke_gate_that_is_green_skips_the_history_read(self):
        statuses = green(gate.SMOKE_CONTEXT) | {gate.SMOKE_GATE: ("success", f"{gate.SMOKE_CONTEXT} passed")}
        api = FakeAPI()
        with mock.patch.object(gate, "smoke_first_reported", side_effect=AssertionError("history read")):
            gate.gate_pull(api, pull(statuses=statuses), not_before=CUTOFF)
        self.assertNotIn(gate.SMOKE_GATE, [payload["context"] for _, payload in api.posts])


class PostTest(unittest.TestCase):
    def test_only_changed_statuses_are_posted(self):
        statuses = green(gate.SMOKE_CONTEXT) | {gate.SMOKE_GATE: ("success", f"{gate.SMOKE_CONTEXT} passed")}
        api = FakeAPI()
        gate.gate_pull(api, pull(statuses=statuses))
        self.assertEqual(sorted(payload["context"] for _, payload in api.posts), [gate.LABELS_GATE])
        self.assertTrue(all(path == f"/repos/{REPO}/statuses/{SHA}" for path, _ in api.posts))

    def test_a_non_green_verdict_is_not_posted_over_nothing(self):
        """Tide held on any non-green context, so a first pending would have held a Tide merge."""
        api = FakeAPI()
        gate.gate_pull(api, pull({"lgtm"}, statuses=green(gate.SMOKE_CONTEXT)))
        self.assertEqual([payload["context"] for _, payload in api.posts], [gate.SMOKE_GATE])

    def test_a_non_green_verdict_does_not_replace_a_green(self):
        """Nothing may re-post it once the hold goes, and the queue judges the labels again anyway."""
        statuses = green(gate.SMOKE_CONTEXT, gate.LABELS_GATE)
        api = FakeAPI()
        gate.gate_pull(api, pull(APPROVED | {"do-not-merge/hold"}, statuses=statuses))
        self.assertNotIn(gate.LABELS_GATE, [payload["context"] for _, payload in api.posts])

    def test_a_non_green_verdict_does_not_replace_a_non_green(self):
        statuses = green(gate.SMOKE_CONTEXT) | {gate.LABELS_GATE: ("pending", "held by do-not-merge/hold")}
        api = FakeAPI()
        gate.gate_pull(api, pull({"approved"}, statuses=statuses))
        self.assertNotIn(gate.LABELS_GATE, [payload["context"] for _, payload in api.posts])

    def test_a_dry_run_posts_nothing(self):
        api = FakeAPI()
        lines = gate.gate_pull(api, pull(statuses=green(gate.SMOKE_CONTEXT)), dry_run=True)
        self.assertEqual(api.posts, [])
        self.assertTrue(any("would post" in line for line in lines))

    def test_a_release_line_is_left_to_tide(self):
        api = FakeAPI()
        gate.gate_pull(api, pull(base="release/1.2"))
        self.assertEqual(api.posts, [])

    def test_the_dry_run_branch_is_gated_like_main(self):
        api = FakeAPI()
        gate.gate_pull(api, pull({"lgtm"}, statuses=green(gate.SMOKE_CONTEXT), base=gate.DRY_RUN_BASE))
        self.assertEqual([payload["context"] for _, payload in api.posts], [gate.SMOKE_GATE])

    def test_a_status_event_gates_the_pull_request_with_that_head(self):
        raw = {"number": 7, "head": {"sha": SHA}, "base": {"ref": "main"}, "labels": [{"name": "lgtm"}]}
        api = FakeAPI(raw_pull=raw, statuses=[{"context": gate.SMOKE_CONTEXT, "state": "success"}])
        gate.cmd_status(api, SHA)
        self.assertEqual([payload["context"] for _, payload in api.posts], [gate.SMOKE_GATE])
        api = FakeAPI(raw_pull=raw)
        gate.cmd_status(api, BASE_SHA)
        self.assertEqual(api.posts, [])


class GroupTest(unittest.TestCase):
    RAW = {"number": 42, "head": {"sha": SHA}, "base": {"ref": "main"}, "labels": [{"name": "lgtm"}, {"name": "approved"}]}

    def test_the_pull_request_is_read_from_the_queue_branch(self):
        ref = f"refs/heads/gh-readonly-queue/main/pr-42-{BASE_SHA}"
        api = FakeAPI(raw_pull=self.RAW)
        passed, line = gate.cmd_group(api, ref, gate.LABELS_GATE)
        self.assertTrue(passed)
        self.assertIn("#42", line)

    def test_anything_short_of_success_fails_the_group(self):
        ref = f"gh-readonly-queue/main/pr-42-{BASE_SHA}"
        api = FakeAPI(raw_pull=self.RAW, files=[{"filename": "a2a/x.go"}])
        self.assertFalse(gate.cmd_group(api, ref, gate.SMOKE_GATE)[0])

    def test_a_ref_that_is_not_a_queue_branch_fails(self):
        for ref in ("refs/heads/main", "refs/heads/gh-readonly-queue/main/pr-42", ""):
            with self.subTest(ref=ref):
                self.assertFalse(gate.cmd_group(FakeAPI(raw_pull=self.RAW), ref, gate.LABELS_GATE)[0])


class EnqueueTest(unittest.TestCase):
    def test_approved_and_not_queued_is_enabled(self):
        self.assertEqual(gate.enqueue_action(pull()), gate.ENABLE)

    def test_already_queued_is_left_alone(self):
        self.assertIsNone(gate.enqueue_action(pull(auto_merge=True)))
        self.assertIsNone(gate.enqueue_action(pull(queue_entry="MQE_1")))

    def test_a_hold_or_a_lost_label_comes_out(self):
        self.assertEqual(gate.enqueue_action(pull(APPROVED | {"do-not-merge/hold"}, auto_merge=True)), gate.DISABLE)
        self.assertEqual(gate.enqueue_action(pull({"approved"}, queue_entry="MQE_1")), gate.DISABLE)

    def test_a_draft_is_never_enabled(self):
        self.assertIsNone(gate.enqueue_action(pull(draft=True)))

    def test_unapproved_and_not_queued_is_nothing(self):
        self.assertIsNone(gate.enqueue_action(pull({"lgtm"})))

    def test_removed_from_the_queue_since_the_last_push_stays_out(self):
        removed = pull() | {"removed_since_push": True}
        self.assertEqual(gate.enqueue_action(removed), gate.LEFT_OUT)


class RemovalTest(unittest.TestCase):
    """The last removal against the head's first check suite, which is when the head was pushed."""

    def test_a_removal_after_the_last_push_counts(self):
        self.assertTrue(gate.removed_since_push(node(1, APPROVED, timeline=[removal()])))

    def test_a_push_after_the_removal_clears_it(self):
        """The commit may be dated before the removal; the check suite is dated by the push."""
        late_push = node(1, APPROVED, timeline=[removal(at="2026-10-13T11:00:00Z")], pushed="2026-10-13T11:20:00Z")
        self.assertFalse(gate.removed_since_push(late_push))

    def test_a_removal_caused_by_the_push_itself_does_not_count(self):
        """A push to a queued pull request removes it within seconds of the push."""
        pushed_while_queued = node(1, APPROVED, timeline=[removal(at="2026-10-13T10:00:30Z")])
        self.assertFalse(gate.removed_since_push(pushed_while_queued))

    def test_a_head_with_no_check_suite_counts_as_pushed_after(self):
        self.assertFalse(gate.removed_since_push(node(1, APPROVED, timeline=[removal()], pushed=None)))

    def test_the_sweeps_own_dequeue_does_not_count(self):
        self.assertFalse(gate.removed_since_push(node(1, APPROVED, timeline=[removal(gate.SWEEP_ACTOR)])))

    def test_only_a_lone_failed_checks_removal_since_the_push_is_retried(self):
        failed = lambda at: removal("github-merge-queue", at=at, reason="failed_checks")
        cases = (
            ("one failed_checks", [failed("2026-10-13T11:00:00Z")], True),
            ("two failed_checks", [failed("2026-10-13T10:30:00Z"), failed("2026-10-13T11:00:00Z")], False),
            ("an earlier one before the push", [failed("2026-10-13T09:00:00Z"), failed("2026-10-13T11:00:00Z")], True),
            ("an earlier one caused by the push", [failed("2026-10-13T10:00:30Z"), failed("2026-10-13T11:00:00Z")], True),
            ("manual", [removal("octocat", reason="manual")], False),
            ("merge_conflict", [removal("github-merge-queue", reason="merge_conflict")], False),
            ("no reason", [removal("github-merge-queue")], False),
            ("a manual one, then failed_checks", [removal("octocat", at="2026-10-13T10:30:00Z", reason="manual"), failed("2026-10-13T11:00:00Z")], True),
            ("failed_checks, then manual", [failed("2026-10-13T10:30:00Z"), removal("octocat", reason="manual")], False),
            ("the sweep's own dequeue last", [failed("2026-10-13T10:30:00Z"), removal(gate.SWEEP_ACTOR, reason="manual")], False),
        )
        for name, timeline, expected in cases:
            with self.subTest(name):
                self.assertEqual(gate.retry_due(node(1, APPROVED, timeline=timeline)), expected)

    def test_a_retry_is_enabled_and_a_spent_one_is_left_out(self):
        base = {"draft": False, "labels": APPROVED, "auto_merge": False, "queue_entry": None, "removed_since_push": True}
        self.assertEqual(gate.enqueue_action({**base, "retry": True}), gate.ENABLE)
        self.assertEqual(gate.enqueue_action({**base, "retry": False}), gate.LEFT_OUT)
        self.assertIsNone(gate.enqueue_action({**base, "retry": True, "labels": {"lgtm"}}))

    def test_no_timeline_item_is_no_removal(self):
        self.assertFalse(gate.removed_since_push(node(1, APPROVED)))

    def test_the_apps_own_dequeue_does_not_count_with_or_without_the_bot_suffix(self):
        self.assertFalse(gate.removed_since_push(node(1, APPROVED, timeline=[removal("merge-bot")]), "merge-bot"))
        self.assertFalse(gate.removed_since_push(node(1, APPROVED, timeline=[removal("merge-bot[bot]")]), "merge-bot"))
        self.assertTrue(gate.removed_since_push(node(1, APPROVED, timeline=[removal(gate.SWEEP_ACTOR)]), "merge-bot"))


def status_node(context, state, description=""):
    return {"__typename": "StatusContext", "context": context, "state": state, "description": description}


PUSHED = "2026-10-13T10:00:00Z"


def removal(actor="octocat", at="2026-10-13T11:00:00Z", reason=None):
    event = {"createdAt": at, "actor": {"__typename": "User", "login": actor}}
    if reason is not None:
        event["reason"] = reason
    return event


#: Two `failed_checks` removals since the push: the retry is spent, so the sweep leaves it out.
FAILED_TWICE = [
    removal("github-merge-queue", at="2026-10-13T10:30:00Z", reason="failed_checks"),
    removal("github-merge-queue", reason="failed_checks"),
]


def node(number, labels, auto_merge=False, queue_entry=None, contexts=(), timeline=(), pushed=PUSHED):
    return {
        "id": f"PR_{number}",
        "number": number,
        "isDraft": False,
        "headRefOid": SHA,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "autoMergeRequest": {"enabledAt": "t"} if auto_merge else None,
        "mergeQueueEntry": {"id": queue_entry} if queue_entry else None,
        "timelineItems": {"nodes": list(timeline)},
        "commits": {"nodes": [{"commit": {
            "checkSuites": {"nodes": [{"createdAt": pushed}] if pushed else []},
            "statusCheckRollup": {"contexts": {"nodes": list(contexts)}},
        }}]},
    }


class RollupTest(unittest.TestCase):
    def test_statuses_read_in_rest_form_and_check_runs_are_skipped(self):
        check_run = {"__typename": "CheckRun", "name": "cla/google", "status": "COMPLETED", "conclusion": "SUCCESS"}
        statuses = gate.rollup_statuses([status_node(gate.SMOKE_CONTEXT, "SUCCESS", "Job succeeded."), check_run])
        self.assertEqual(statuses, {gate.SMOKE_CONTEXT: ("success", "Job succeeded.")})


class SweepTest(unittest.TestCase):
    def test_enables_dequeues_and_refreshes_only_the_approved(self):
        api = FakeAPI(
            nodes=[
                node(1, APPROVED, contexts=[status_node(gate.SMOKE_CONTEXT, "SUCCESS")]),
                node(2, {"lgtm", "do-not-merge/hold"}, auto_merge=True, queue_entry="MQE_2"),
                node(3, {"lgtm"}),
            ]
        )
        lines, failures = gate.sweep(api)
        self.assertEqual(failures, 0)
        self.assertIn(("enablePullRequestAutoMerge", "PR_1"), api.mutations)
        self.assertIn(("dequeuePullRequest", "PR_2"), api.mutations)
        self.assertIn(("disablePullRequestAutoMerge", "PR_2"), api.mutations)
        self.assertEqual(len(api.mutations), 3)
        self.assertEqual({path for path, _ in api.posts}, {f"/repos/{REPO}/statuses/{SHA}"})
        self.assertEqual(len(api.posts), 2, "only #1 is approved, so only #1's statuses are refreshed")

    def test_a_status_already_posted_reads_as_unchanged(self):
        """GraphQL says SUCCESS; a gate already green must not be posted again."""
        posted = status_node(gate.LABELS_GATE, "SUCCESS", gate.labels_verdict(APPROVED)[1])
        api = FakeAPI(nodes=[node(1, APPROVED, auto_merge=True, contexts=[posted])])
        gate.sweep(api)
        self.assertNotIn(gate.LABELS_GATE, [payload["context"] for _, payload in api.posts])

    def test_the_calls_that_enqueue_go_through_the_enqueue_token(self):
        api, enqueuer = FakeAPI(nodes=[node(1, APPROVED)]), FakeAPI()
        gate.sweep(api, enqueuer=enqueuer)
        self.assertEqual((api.mutations, enqueuer.mutations), ([], [("enablePullRequestAutoMerge", "PR_1")]))

    def test_a_write_sweep_needs_the_enqueue_token_and_a_dry_run_does_not(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True), mock.patch.object(gate, "sweep", return_value=([], 0)):
            self.assertEqual(gate.main(["--repo", REPO, "sweep"]), 2)
            self.assertEqual(gate.main(["--repo", REPO, "--dry-run", "sweep", "--base", gate.DRY_RUN_BASE]), 0)

    def test_a_pull_request_the_queue_removed_is_not_enabled_again(self):
        api = FakeAPI(nodes=[node(1, APPROVED, timeline=[removal()])])
        lines, failures = gate.sweep(api)
        self.assertEqual((failures, api.mutations), (0, []))
        self.assertTrue(any("left out" in line for line in lines))

    def test_a_left_out_line_carries_the_reason_and_the_re_add_command(self):
        api = FakeAPI(nodes=[node(7, APPROVED, timeline=FAILED_TWICE)])
        lines, _ = gate.sweep(api)
        self.assertIn(
            "#7: left out, removed 2026-10-13T11:00:00Z by github-merge-queue (reason: failed_checks). "
            "To re-add (a write user): gh api graphql -f query='mutation { enablePullRequestAutoMerge("
            'input: {pullRequestId: "PR_7"}) { clientMutationId } }\', '
            "or push a fix (a push drops lgtm unless the author is in gke-labs/Googlers).",
            lines,
        )

    def test_a_left_out_line_names_whatever_reason_the_queue_gave(self):
        for reason, shown in (("failed_checks", "failed_checks"), ("manual", "manual"), (None, "unknown"), ("", "unknown")):
            with self.subTest(reason=reason):
                pull = {"number": 7, "id": "PR_7", "removal": removal("octocat", reason=reason)}
                self.assertIn(f"(reason: {shown})", gate.left_out_line(pull))

    def test_a_left_out_line_survives_a_removal_with_no_actor(self):
        pull = {"number": 7, "id": "PR_7", "removal": {"createdAt": "2026-10-13T11:00:00Z", "actor": None}}
        line = gate.left_out_line(pull)
        self.assertIn("by unknown (reason: unknown)", line)
        self.assertIn('pullRequestId: "PR_7"', line)

    def test_the_printed_re_add_is_the_sweeps_own_enable_call(self):
        # The pasted command must make the same call the sweep makes, on the same pull request.
        # No single quote inside, so the shell's single quotes around it hold.
        query = gate.ENABLE_COMMAND_QUERY.format(id="PR_7")
        self.assertIn("enablePullRequestAutoMerge(input: {pullRequestId: $id})", gate.ENABLE_MUTATION)
        self.assertIn('enablePullRequestAutoMerge(input: {pullRequestId: "PR_7"})', query)
        self.assertNotIn("'", query)

    def test_a_left_out_pull_request_gets_one_comment_with_the_line(self):
        api, enqueuer = FakeAPI(nodes=[node(7, APPROVED, timeline=FAILED_TWICE)]), FakeAPI()
        lines, failures = gate.sweep(api, enqueuer=enqueuer)
        self.assertEqual(failures, 0)
        comments = [(path, payload["body"]) for path, payload in api.posts if path.endswith("/comments")]
        self.assertEqual([path for path, _ in comments], [f"/repos/{REPO}/issues/7/comments"])
        body = comments[0][1]
        self.assertTrue(body.startswith("<!-- merge-queue-left-out 2026-10-13T11:00:00Z -->\n"))
        self.assertIn(gate.left_out_line(gate.graphql_pulls(api)[0]), body)
        self.assertIn("```shell\ngh api graphql -f query='" + gate.ENABLE_COMMAND_QUERY.format(id="PR_7") + "'\n```", body)
        self.assertEqual(enqueuer.posts, [], "the comment goes through GITHUB_TOKEN, not the App")
        self.assertIn("#7: posted the left-out comment", lines)
        self.assertEqual(api.comment_reads, [f"/repos/{REPO}/issues/7/comments?since=2026-10-13T11:00:00Z"])
        # Prow reads a slash command at the start of a line; no line of the comment may start one.
        self.assertFalse(any(line.startswith("/") for line in body.splitlines()))

    def _comment_for(self, reason, actor="github-merge-queue"):
        pull = {"number": 7, "id": "PR_7", "removal": removal(actor, reason=reason)}
        return gate.left_out_comment(pull, "<!-- marker -->", gate.left_out_line(pull))

    def test_every_comment_carries_the_line_and_the_lgtm_note(self):
        for reason in ("failed_checks", "manual", "merge_conflict", "something_new", None):
            with self.subTest(reason=reason):
                pull = {"number": 7, "id": "PR_7", "removal": removal("octocat", reason=reason)}
                body = gate.left_out_comment(pull, "<!-- marker -->", gate.left_out_line(pull))
                self.assertTrue(body.startswith("<!-- marker -->\nThis pull request was removed from the merge queue"))
                self.assertIn("```text\n" + gate.left_out_line(pull) + "\n```", body)
                self.assertIn("A push keeps `lgtm` for members of gke-labs/Googlers. Other authors may need a new `/lgtm`.", body)
                self.assertFalse(any(line.startswith("/") for line in body.splitlines()))

    def test_a_second_failed_check_asks_for_a_fix_or_an_empty_push_then_a_write_user(self):
        body = self._comment_for("failed_checks")
        self.assertIn("because a required check failed again, after the one retry", body)
        self.assertIn("If a check found a real problem, push a fix.", body)
        self.assertIn("If it was a flake, push an empty commit, and the sweep puts it back once its checks pass:", body)
        self.assertIn('```shell\ngit commit --allow-empty -m "Re-enter the merge queue" && git push\n```', body)
        self.assertIn("If that does not work, ask a write user to run this command:", body)
        self.assertIn("```shell\ngh api graphql -f query='" + gate.ENABLE_COMMAND_QUERY.format(id="PR_7") + "'\n```", body)
        self.assertLess(body.index("push an empty commit"), body.index("ask a write user"))

    def test_a_manual_removal_names_who_and_asks_for_a_push_or_a_hold(self):
        body = self._comment_for("manual", actor="octocat")
        self.assertIn("removed from the merge queue by @octocat.", body)
        self.assertIn("If @octocat did not mean to keep it out, push (an empty commit is enough), and the sweep puts it back once its checks pass:", body)
        self.assertIn("git commit --allow-empty", body)
        self.assertIn("If they did mean to keep it out, they should add a hold label (`/hold`), so that it stays out after the next push.", body)
        self.assertNotIn("ask a write user", body)

    def test_only_a_user_is_mentioned(self):
        app = {"number": 7, "id": "PR_7", "removal": {"createdAt": "t", "reason": "manual", "actor": {"__typename": "Bot", "login": "some-app[bot]"}}}
        body = gate.left_out_comment(app, "<!-- marker -->", gate.left_out_line(app))
        self.assertIn("removed from the merge queue by `some-app`.", body)
        self.assertNotIn("@some-app", body)
        nobody = {"number": 7, "id": "PR_7", "removal": {"createdAt": "t", "reason": "manual", "actor": None}}
        body = gate.left_out_comment(nobody, "<!-- marker -->", gate.left_out_line(nobody))
        self.assertIn("removed from the merge queue by unknown.", body)
        self.assertNotIn("@unknown", body)

    def test_a_merge_conflict_asks_for_a_rebase(self):
        body = self._comment_for("merge_conflict")
        self.assertIn("because it conflicts with `main`, or with a pull request ahead of it in the queue.", body)
        self.assertIn("Rebase onto `main`, fix any conflict, and push. The sweep puts it back after its checks pass.", body)
        self.assertNotIn("--allow-empty", body)

    def test_another_reason_is_named_and_gets_the_push_then_write_user_advice(self):
        body = self._comment_for("something_new")
        self.assertIn("removed from the merge queue (reason: something_new).", body)
        self.assertIn("git commit --allow-empty", body)
        self.assertIn("ask a write user to run this command:", body)

    def test_a_failed_comment_read_is_logged_and_does_not_fail_the_sweep(self):
        api = FakeAPI(nodes=[node(7, APPROVED, timeline=[removal()])])
        api.refuse_comment_reads = True
        lines, failures = gate.sweep(api)
        self.assertEqual(failures, 0)
        self.assertIn("#7: left-out comment not posted: HTTP 502", lines)
        self.assertEqual([path for path, _ in api.posts if path.endswith("/comments")], [])

    def test_a_refused_comment_is_logged_and_does_not_fail_the_sweep(self):
        api = FakeAPI(nodes=[node(7, APPROVED, timeline=[removal()])])
        api.refuse_comments = True
        lines, failures = gate.sweep(api)
        self.assertEqual(failures, 0)
        self.assertIn("#7: left-out comment not posted: HTTP 403", lines)
        self.assertTrue(any(line.startswith("#7: left out, removed") for line in lines))

    def test_the_same_removal_is_not_commented_twice(self):
        posted = {"body": "<!-- merge-queue-left-out 2026-10-13T11:00:00Z -->\nThe merge queue removed this pull request."}
        api = FakeAPI(nodes=[node(7, APPROVED, timeline=[removal()])], comments=[{"body": "lgtm"}, posted])
        lines, _ = gate.sweep(api)
        self.assertEqual([path for path, _ in api.posts if path.endswith("/comments")], [])
        self.assertIn("#7: left-out comment already posted", lines)

    def test_a_later_removal_gets_a_new_comment(self):
        earlier = {"body": "<!-- merge-queue-left-out 2026-10-13T11:00:00Z -->\nThe merge queue removed this pull request."}
        later = removal(at="2026-10-14T09:00:00Z")
        api = FakeAPI(nodes=[node(7, APPROVED, timeline=[later])], comments=[earlier])
        gate.sweep(api)
        bodies = [payload["body"] for path, payload in api.posts if path.endswith("/comments")]
        self.assertEqual(len(bodies), 1)
        self.assertTrue(bodies[0].startswith("<!-- merge-queue-left-out 2026-10-14T09:00:00Z -->"))

    def test_a_dry_run_posts_no_left_out_comment(self):
        api = FakeAPI(nodes=[node(7, APPROVED, timeline=[removal()])])
        lines, _ = gate.sweep(api, dry_run=True)
        self.assertEqual(api.posts, [])
        self.assertIn("#7: would post the left-out comment", lines)

    def test_the_first_failed_checks_removal_is_retried_once(self):
        api = FakeAPI(nodes=[node(7, APPROVED, timeline=[removal("github-merge-queue", reason="failed_checks")])])
        lines, failures = gate.sweep(api)
        self.assertEqual((failures, api.mutations), (0, [("enablePullRequestAutoMerge", "PR_7")]))
        self.assertIn("#7: auto-merge on again, the one retry after a failed_checks removal", lines)
        self.assertEqual([path for path, _ in api.posts if "/comments" in path], [], "no comment before the retry fails")

    def test_a_second_failed_checks_removal_is_left_out_and_commented(self):
        api = FakeAPI(nodes=[node(7, APPROVED, timeline=FAILED_TWICE)])
        lines, _ = gate.sweep(api)
        self.assertEqual(api.mutations, [])
        self.assertTrue(any(line.startswith("#7: left out, removed 2026-10-13T11:00:00Z") for line in lines))
        self.assertIn("#7: posted the left-out comment", lines)

    def test_the_query_reads_each_size_into_its_own_connection(self):
        api = FakeAPI()
        with mock.patch.object(gate, "REMOVALS_READ", 3), mock.patch.object(gate, "CHECK_SUITES_READ", 7):
            gate.graphql_pulls(api)
        self.assertIn("timelineItems(last: 3,", api.queries[0])
        self.assertIn("checkSuites(first: 7)", api.queries[0])

    def test_a_dry_run_changes_nothing(self):
        api = FakeAPI(nodes=[node(1, APPROVED)])
        lines, _ = gate.sweep(api, dry_run=True)
        self.assertEqual((api.mutations, api.posts), ([], []))
        self.assertTrue(any("would turn auto-merge on" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
