"""Unit tests for the cron→kanban dispatch bridge.

Run: python3 -m unittest agents/chat/scripts/test_platform_cron_dispatch.py

`platform_cron_dispatch.py` is what makes the Platform Agent's cron roster fire
at all: the Chat Agent profile owns the only ticking gateway, so its scheduler
files a card and a Platform Agent worker does the work. Two properties keep
that honest, and both are tested here.

**Nothing reaches chat by accident.** A `no_agent` job's stdout is delivered
verbatim to the user, so every path through `main` must leave stdout empty —
including the paths that decided to do nothing.

**One tick, at most one run.** The bridge sits between a schedule and an
expensive fleet-wide audit, so a duplicate card means the same audit running
twice against the same cluster and writing the same ledger issue. The dedup
has two independent layers: an idempotency key that catches two dispatches of
one tick, and an open-card check that catches a tick arriving while the last
one is still working.

The wiring to the board is stubbed at `_run_slash`, the single seam through
which every kanban call goes.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import platform_cron_dispatch as pcd  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
PLATFORM_ROSTER = REPO / "agents/platform/cron/jobs.json"
CHAT_ROSTER = REPO / "agents/chat/defaults/cron/jobs.json"
SCRIPTS_DIR = Path(__file__).resolve().parent

ROSTER = {
    "jobs": [
        {"id": "compliance-audit", "name": "Security & RBAC Posture Audit", "enabled": True},
        {"id": "retired-audit", "name": "Retired Audit", "enabled": False},
    ]
}


class DispatchHarness(unittest.TestCase):
    """Base fixture: a roster on disk and a recording stub for the board."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.roster_path = Path(self._tmp.name) / "jobs.json"
        self.roster_path.write_text(json.dumps(ROSTER), encoding="utf-8")

        self.calls: list[str] = []
        self.list_response = "[]"
        self.create_response = '{"id": "t_abc123"}'
        # None means "answer the way the real CLI does": one `Archived <id>`
        # line per id, which is what archive_cards counts.
        self.archive_response: str | None = None
        self.raises: Exception | None = None
        self.create_raises: Exception | None = None

        self._real_run_slash = pcd._run_slash
        pcd._run_slash = self._fake_run_slash

    def tearDown(self):
        pcd._run_slash = self._real_run_slash
        self._tmp.cleanup()

    def _fake_run_slash(self, cmd: str) -> str:
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        if cmd.startswith("list"):
            return self.list_response
        if cmd.startswith("archive"):
            if self.archive_response is not None:
                return self.archive_response
            return "\n".join(f"Archived {t}" for t in cmd.split()[1:])
        if self.create_raises is not None:
            raise self.create_raises
        return self.create_response

    def run_main(self, job_id="compliance-audit") -> tuple[int, str]:
        """Run main, returning (exit code, anything it printed to stdout)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = pcd.main(job_id, roster_paths=(self.roster_path,))
        return rc, buf.getvalue()

    def run_main_stderr(self, job_id="compliance-audit") -> tuple[int, str]:
        """Run main, returning (exit code, anything it logged to stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = pcd.main(job_id, roster_paths=(self.roster_path,))
        return rc, err.getvalue()

    @property
    def create_calls(self) -> list[str]:
        return [c for c in self.calls if c.startswith("create")]

    @property
    def archive_calls(self) -> list[str]:
        return [c for c in self.calls if c.startswith("archive")]

    def board(self, *cards: tuple[str, str, int], title="Security & RBAC Posture Audit",
              job_id="compliance-audit"):
        """Set the board listing from (id, status, created_at) triples."""
        self.list_response = json.dumps(
            [
                {"id": i, "title": pcd.card_title(job_id, title), "status": s, "created_at": ts}
                for i, s, ts in cards
            ]
        )


class SilenceTest(DispatchHarness):
    """Stdout is the chat wire. Every path must leave it empty."""

    def test_filing_a_card_prints_nothing(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(len(self.create_calls), 1)

    def test_skipping_a_disabled_job_prints_nothing(self):
        rc, out = self.run_main("retired-audit")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(self.create_calls, [])

    def test_board_failure_prints_nothing(self):
        self.raises = RuntimeError("board is down")
        rc, out = self.run_main()
        # Exit 0, not 1: a board outage is transient and the next tick will
        # retry. Alerting the user every 30 minutes until it clears would
        # train them to ignore the alert.
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")


class RosterLookupTest(DispatchHarness):
    """The roster is the source of truth for what may be dispatched."""

    def test_unknown_job_id_is_an_error_not_a_card(self):
        rc, out = self.run_main("no-such-job")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(self.create_calls, [])

    def test_missing_roster_is_an_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = pcd.main("compliance-audit", roster_paths=(Path("/nonexistent.json"),))
        self.assertEqual(rc, 1)
        self.assertEqual(buf.getvalue(), "")

    def test_the_missing_roster_alert_names_the_paths_it_tried(self):
        # The alert is the only thing an operator gets, and it used to print the
        # module default whatever was actually opened — sending them to two
        # files that were never read.
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            pcd.main("compliance-audit", roster_paths=(Path("/nowhere/jobs.json"),))
        self.assertIn("/nowhere/jobs.json", err.getvalue())

    def test_first_readable_roster_wins(self):
        loaded = pcd.load_roster((Path("/nonexistent.json"), self.roster_path))
        self.assertIn("compliance-audit", loaded)

    def test_disabling_the_job_in_the_platform_roster_disables_the_trigger(self):
        # The trigger must not outlive the job it triggers. `enabled: false` is
        # the documented way to retire a watchdog, and it is set on the
        # Platform Agent's entry — which this side has to honour, or a retired
        # audit keeps being dispatched by a card that names it.
        rc, _ = self.run_main("retired-audit")
        self.assertEqual(rc, 0)
        self.assertEqual(self.create_calls, [])

    def test_card_title_uses_the_roster_name(self):
        self.run_main()
        self.assertIn("Run the Security & RBAC Posture Audit cron job", self.create_calls[0])


class DedupTest(DispatchHarness):
    """Two layers stop the same audit running twice."""

    def test_idempotency_key_is_per_tick_not_per_job(self):
        # A fixed key would match the first card forever, and the job would run
        # exactly once in the lifetime of the board.
        t1 = datetime(2026, 8, 6, 6, 20, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 7, 6, 20, tzinfo=timezone.utc)
        self.assertNotEqual(
            pcd.idempotency_key("compliance-audit", t1),
            pcd.idempotency_key("compliance-audit", t2),
        )

    def test_idempotency_key_collapses_two_dispatches_of_one_tick(self):
        same_minute_a = datetime(2026, 8, 6, 6, 20, 1, tzinfo=timezone.utc)
        same_minute_b = datetime(2026, 8, 6, 6, 20, 59, tzinfo=timezone.utc)
        self.assertEqual(
            pcd.idempotency_key("compliance-audit", same_minute_a),
            pcd.idempotency_key("compliance-audit", same_minute_b),
        )

    def test_no_card_while_an_earlier_one_is_running(self):
        self.list_response = json.dumps(
            [{"id": "t_old", "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "running"}]
        )
        rc, out = self.run_main()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.create_calls, [])

    def test_a_finished_card_does_not_block_the_next_tick(self):
        self.list_response = json.dumps(
            [{"id": "t_old", "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "done"}]
        )
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)

    def test_a_blocked_card_does_not_block_the_next_tick(self):
        # Deliberate: a blocked card waits on a human, and treating it as
        # in-flight would let one bad run switch the audit off indefinitely —
        # silently, which is exactly the failure this bridge exists to end.
        self.list_response = json.dumps(
            [{"id": "t_old", "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "blocked"}]
        )
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)

    def test_another_jobs_open_card_does_not_block_this_one(self):
        self.list_response = json.dumps(
            [{"id": "t_other", "title": pcd.card_title("fleet-wide-cost-analysis", "Fleet Waste Audit"),
              "status": "running"}]
        )
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)

    def test_an_unreadable_board_fails_open(self):
        self.list_response = "not json"
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)


class DedupHandleTest(DispatchHarness):
    """The handle that recognises this job's cards must outlive a rename."""

    def test_the_title_carries_the_job_id(self):
        self.run_main()
        self.assertIn("[compliance-audit]", self.create_calls[0])

    def test_a_renamed_job_still_sees_its_in_flight_card(self):
        # The docs promise the id is stable and the name is not. Keyed on the
        # name, the first tick after a rename cannot see the running card and
        # files a second one — two copies of the same audit, concurrently.
        self.roster_path.write_text(
            json.dumps({"jobs": [{"id": "compliance-audit", "name": "Renamed Audit",
                                  "enabled": True}]}),
            encoding="utf-8",
        )
        self.list_response = json.dumps(
            [{"id": "t_old",
              "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "running"}]
        )
        rc, out = self.run_main()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.create_calls, [])

    def test_cards_filed_before_the_id_existed_are_still_swept(self):
        # Without the bare-name fallback, adding the id would strand every card
        # already on the board — the exact backlog the sweep exists to prevent.
        legacy = [
            {"id": f"t_{i}", "title": "Run the Security & RBAC Posture Audit cron job",
             "status": "done", "created_at": i}
            for i in range(pcd.KEEP_FINISHED + 2)
        ]
        self.list_response = json.dumps(legacy)
        self.run_main()
        self.assertEqual(self.archive_calls[0].split()[1:], ["t_0", "t_1"])

    def test_one_job_id_is_not_a_suffix_of_another(self):
        # `endswith("[audit]")` must not match "[compliance-audit]"; the bracket
        # is what keeps two jobs from sharing a dedup handle.
        title = pcd.card_title("compliance-audit", "Security & RBAC Posture Audit")
        self.assertFalse(pcd._is_this_jobs_card(title, "audit", "Some Other Audit"))
        self.assertTrue(pcd._is_this_jobs_card(title, "compliance-audit", "Anything"))


class UnknownStatusTest(DispatchHarness):
    """The board's vocabulary is not schema-constrained, so surprises must show."""

    def _card(self, status: str) -> str:
        return json.dumps(
            [{"id": "t_odd",
              "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": status, "created_at": 1}]
        )

    def test_completed_is_a_real_status_not_a_guess(self):
        # It is absent from kanban_db.VALID_STATUSES but present on live boards,
        # which is the whole reason unknown statuses get logged.
        self.assertIn("completed", pcd.FINISHED)
        self.assertIn("completed", pcd.KNOWN_STATUSES)

    def test_an_unrecognised_status_is_logged(self):
        self.list_response = self._card("half_eaten")
        rc, err = self.run_main_stderr()
        self.assertEqual(rc, 0)
        self.assertIn("t_odd=half_eaten", err)

    def test_an_unrecognised_status_still_fails_open(self):
        # Logging is the fix, not a stricter default: treating the unknown as
        # in-flight would let one odd card retire the audit permanently.
        self.list_response = self._card("half_eaten")
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)
        self.assertEqual(self.archive_calls, [])

    def test_the_statuses_we_handle_are_not_reported_as_unknown(self):
        for status in sorted(pcd.KNOWN_STATUSES):
            with self.subTest(status=status):
                self.calls.clear()
                self.list_response = self._card(status)
                _, err = self.run_main_stderr()
                self.assertNotIn("does not know", err)


class AlertingTest(DispatchHarness):
    """A non-zero exit is a page. It must fire for defects and only defects."""

    def test_a_create_the_board_refuses_alerts(self):
        # The listing succeeded, so the board is up and talking and it still
        # said no. That will say no on every future tick; exiting 0 would
        # retire the audit for good with one stderr line as the only trace.
        self.create_raises = RuntimeError("unrecognized arguments: --max-runtime")
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")

    def test_a_create_failure_on_an_unreachable_board_stays_quiet(self):
        # Both calls fail: the board is down, which is weather. The next tick
        # retries, and paging every 30 minutes until it clears trains people to
        # ignore the page.
        self.raises = RuntimeError("board is down")
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)

    def test_an_in_flight_card_is_not_an_alert(self):
        self.list_response = json.dumps(
            [{"id": "t_old",
              "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "running"}]
        )
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)

    def test_an_unreadable_task_id_is_not_an_alert(self):
        # The card is very likely on the board; only its id came back
        # unreadable, and nothing downstream uses the id.
        self.create_response = "not a task"
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)


class RetentionTest(DispatchHarness):
    """Every tick files a card, so every tick must also take one away."""

    def finished(self, n: int) -> list[tuple[str, str, int]]:
        return [(f"t_{i:02d}", "done", 1000 + i) for i in range(n)]

    def test_a_short_history_is_left_alone(self):
        self.board(*self.finished(pcd.KEEP_FINISHED))
        self.run_main()
        self.assertEqual(self.archive_calls, [])

    def test_the_surplus_is_archived_oldest_first(self):
        # 48 ticks a day at the resolver's cadence: without this the board grows
        # without bound and the useful cards stop being findable.
        self.board(*self.finished(pcd.KEEP_FINISHED + 3))
        self.run_main()
        self.assertEqual(len(self.archive_calls), 1)
        self.assertEqual(self.archive_calls[0].split()[1:], ["t_00", "t_01", "t_02"])

    def test_the_newest_finished_cards_survive(self):
        self.board(*self.finished(pcd.KEEP_FINISHED + 1))
        self.run_main()
        archived = self.archive_calls[0].split()[1:]
        for card in self.finished(pcd.KEEP_FINISHED + 1)[-pcd.KEEP_FINISHED :]:
            self.assertNotIn(card[0], archived)

    def test_listing_order_does_not_decide_who_goes(self):
        # The board hands rows back in whatever order it likes; age is the only
        # thing that may choose, or a reordered listing archives the newest card.
        self.board(("t_new", "done", 9000), ("t_old", "done", 1), ("t_mid", "done", 500))
        pcd.KEEP_FINISHED, keep = 1, pcd.KEEP_FINISHED
        try:
            self.run_main()
        finally:
            pcd.KEEP_FINISHED = keep
        self.assertEqual(self.archive_calls[0].split()[1:], ["t_old", "t_mid"])

    def test_blocked_cards_are_never_archived(self):
        # A blocked card is the only durable sign a job needs a human. Sweeping
        # it up would erase the one thing worth keeping.
        self.board(("t_blocked", "blocked", 1), *self.finished(pcd.KEEP_FINISHED + 2))
        self.run_main()
        self.assertNotIn("t_blocked", self.archive_calls[0])

    def test_another_jobs_cards_are_not_swept_up(self):
        self.board(*self.finished(pcd.KEEP_FINISHED + 2))
        theirs = json.loads(self.list_response)
        theirs.append({"id": "t_theirs", "title": pcd.card_title("fleet-wide-cost-analysis", "Fleet Waste Audit"),
                       "status": "done", "created_at": 1})
        self.list_response = json.dumps(theirs)
        self.run_main()
        self.assertNotIn("t_theirs", self.archive_calls[0])

    def test_an_in_flight_card_defers_the_sweep(self):
        # Nothing was filed, so nothing needs making room for; the next tick
        # that does file will prune. One listing decides both.
        self.board(("t_running", "running", 9000), *self.finished(pcd.KEEP_FINISHED + 2))
        self.run_main()
        self.assertEqual(self.create_calls, [])
        self.assertEqual(self.archive_calls, [])

    def test_a_failed_create_still_prunes(self):
        self.board(*self.finished(pcd.KEEP_FINISHED + 1))
        self.create_response = "not a task"
        rc, out = self.run_main()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(len(self.archive_calls), 1)

    def test_an_archive_that_swept_nothing_is_not_logged_as_success(self):
        # The log line is the only signal anyone gets. `kanban archive` writes
        # its refusals to stderr, which shares this buffer, so a call that
        # archived nothing still returns normally — and the log used to say
        # "archived 2" while the board went on growing.
        self.board(*self.finished(pcd.KEEP_FINISHED + 2))
        self.archive_response = "cannot archive t_00\ncannot archive t_01"
        rc, err = self.run_main_stderr()
        self.assertEqual(rc, 0)
        self.assertIn("confirmed 0 of 2", err)
        self.assertNotIn("archived 2 finished card(s)", err)

    def test_a_fully_confirmed_archive_is_logged_as_success(self):
        self.board(*self.finished(pcd.KEEP_FINISHED + 2))
        rc, err = self.run_main_stderr()
        self.assertEqual(rc, 0)
        self.assertIn("archived 2 finished card(s)", err)

    def test_a_failed_archive_does_not_fail_the_tick(self):
        original = self._fake_run_slash

        def flaky(cmd: str) -> str:
            if cmd.startswith("archive"):
                self.calls.append(cmd)
                raise RuntimeError("board is busy")
            return original(cmd)

        pcd._run_slash = flaky
        self.board(*self.finished(pcd.KEEP_FINISHED + 1))
        rc, out = self.run_main()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(len(self.create_calls), 1)


class CardContentTest(DispatchHarness):
    """The card names the job; it never carries a copy of the job's prompt."""

    def test_body_dispatches_by_job_id(self):
        self.assertIn(
            "cronjob(action='run', job_id='compliance-audit')",
            pcd.card_body("compliance-audit"),
        )

    def test_body_carries_no_copy_of_the_audit_prompt(self):
        # Duplicating the prompt here would fork the definition of the audit in
        # two files, and the copy would be the one that quietly went stale.
        platform_prompt = next(
            j["prompt"]
            for j in json.loads(PLATFORM_ROSTER.read_text(encoding="utf-8"))["jobs"]
            if j["id"] == "compliance-audit"
        )
        body = pcd.card_body("compliance-audit")
        self.assertNotIn(platform_prompt[:60], body)
        self.assertNotIn("compliance_audit_sop.md", body)

    def test_card_is_attributed_to_cron(self):
        self.run_main()
        self.assertIn("--created-by cron", self.create_calls[0])

    def test_resolver_runtime_cap_is_under_its_own_period(self):
        # It reruns every 30 minutes. A cap at or above that lets a wedged run
        # still hold the board when the next tick arrives.
        self.assertEqual(pcd.MAX_RUNTIME["github-issue-resolver"], "25m")


class WiringTest(unittest.TestCase):
    """The three files that have to agree: chat roster, wrappers, platform roster."""

    def setUp(self):
        self.chat_jobs = json.loads(CHAT_ROSTER.read_text(encoding="utf-8"))["jobs"]
        self.platform_jobs = json.loads(PLATFORM_ROSTER.read_text(encoding="utf-8"))["jobs"]
        self.dispatch_jobs = [j for j in self.chat_jobs if j["id"].startswith("dispatch-")]

    def test_every_platform_job_has_a_trigger(self):
        self.assertEqual(
            sorted(f"dispatch-{j['id']}" for j in self.platform_jobs),
            sorted(j["id"] for j in self.dispatch_jobs),
        )

    def test_triggers_keep_the_platform_schedules(self):
        by_id = {j["id"]: j for j in self.platform_jobs}
        for job in self.dispatch_jobs:
            source = by_id[job["id"].removeprefix("dispatch-")]
            self.assertEqual(job["schedule"], source["schedule"], job["id"])

    def test_triggers_are_script_jobs_that_never_prompt_a_model(self):
        # The Chat Agent has no `terminal` and no `skills` toolset, so a
        # prompt-carrying job here could not run an audit even if it tried.
        for job in self.dispatch_jobs:
            self.assertTrue(job["no_agent"], job["id"])
            self.assertEqual(job["prompt"], "", job["id"])
            self.assertEqual(job["deliver"], "local", job["id"])

    def test_every_trigger_points_at_a_wrapper_that_exists(self):
        for job in self.dispatch_jobs:
            wrapper = SCRIPTS_DIR / job["script"]
            self.assertTrue(wrapper.is_file(), f"{job['id']} -> {job['script']}")
            self.assertIn(
                f'main("{job["id"].removeprefix("dispatch-")}")',
                wrapper.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
