"""Unit tests for the cron→kanban dispatch bridge.

Run: python3 -m unittest agents/chat/scripts/test_platform_cron_dispatch.py

`platform_cron_dispatch.py` is what makes the governance jobs fire at all: the
Chat Agent profile owns the only ticking gateway, so the jobs live in its cron
roster, its scheduler files a card, and a Platform Agent worker does the work.
Two properties keep that honest, and both are tested here.

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

PROMPT = "Run the daily fleet security and RBAC posture audit. Read the SOP."

ROSTER = {
    "jobs": [
        {"id": "compliance-audit", "name": "Security & RBAC Posture Audit",
         "prompt": PROMPT, "skills": ["fleet-audit"], "enabled": True},
        {"id": "retired-audit", "name": "Retired Audit", "prompt": PROMPT,
         "enabled": False},
        {"id": "bodyless-audit", "name": "Bodyless Audit", "prompt": "",
         "enabled": True},
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
                {"id": i, "title": pcd.card_title(job_id, title), "status": s,
                 "created_at": ts, "created_by": pcd.CREATED_BY}
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

    def stale_roster(self) -> Path:
        """A roster that parses fine and predates the job being asked for.

        What an upgraded cluster's volume copy is: the scheduler rewrites it
        every tick, so the entrypoint's `cp -ru` never refreshes it from the
        image again.
        """
        path = Path(self._tmp.name) / "stale.json"
        path.write_text(
            json.dumps({"jobs": [{"id": "some-older-job", "prompt": "x"}]}),
            encoding="utf-8",
        )
        return path

    def test_a_stale_first_roster_does_not_hide_the_job_in_a_later_one(self):
        # "First readable" defeated the /opt/defaults fallback in exactly the
        # case it exists for — the hand-run an operator reaches for *because*
        # the volume's roster is missing the entry.
        loaded = pcd.load_roster((self.stale_roster(), self.roster_path), "compliance-audit")
        self.assertIn("compliance-audit", loaded)

    def test_the_stale_roster_still_wins_when_it_has_the_job(self):
        # Live state beats the image everywhere else: an operator's local edit
        # to a shipped entry has to keep taking effect.
        edited = Path(self._tmp.name) / "edited.json"
        edited.write_text(
            json.dumps({"jobs": [{"id": "compliance-audit", "prompt": "edited locally"}]}),
            encoding="utf-8",
        )
        loaded = pcd.load_roster((edited, self.roster_path), "compliance-audit")
        self.assertEqual(loaded["compliance-audit"]["prompt"], "edited locally")

    def test_a_job_in_no_roster_reports_the_one_the_cluster_runs(self):
        # Nothing to fall through to, so the message describes the live roster
        # rather than an empty dict.
        loaded = pcd.load_roster((self.stale_roster(), self.roster_path), "no-such-job")
        self.assertIn("some-older-job", loaded)

    def test_a_job_found_only_in_the_fallback_is_dispatchable(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = pcd.main(
                "compliance-audit",
                roster_paths=(self.stale_roster(), self.roster_path),
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.create_calls), 1)

    def test_a_disabled_job_files_nothing(self):
        # `enabled: false` is the documented way to retire a watchdog. The
        # scheduler already honours it, so this only answers a hand-run of the
        # wrapper — but a hand-run that files a card for a retired audit would
        # be a real way to resurrect one.
        rc, _ = self.run_main("retired-audit")
        self.assertEqual(rc, 0)
        self.assertEqual(self.create_calls, [])

    def test_a_job_with_no_prompt_is_an_error_not_an_empty_card(self):
        # The prompt IS the work now that nothing else holds a copy of it. A
        # card with an empty body would spawn a worker to improvise the audit
        # or give up; both are worse than an alert naming the broken entry.
        rc, out = self.run_main("bodyless-audit")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(self.create_calls, [])

    def test_card_title_uses_the_roster_name(self):
        self.run_main()
        self.assertIn("Security & RBAC Posture Audit [compliance-audit]", self.create_calls[0])


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
              "status": "running", "created_by": pcd.CREATED_BY}]
        )
        rc, out = self.run_main()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.create_calls, [])

    def test_a_finished_card_does_not_block_the_next_tick(self):
        self.list_response = json.dumps(
            [{"id": "t_old", "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "done", "created_by": pcd.CREATED_BY}]
        )
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)

    def test_a_blocked_card_does_not_block_the_next_tick(self):
        # Deliberate: a blocked card waits on a human, and treating it as
        # in-flight would let one bad run switch the audit off indefinitely —
        # silently, which is exactly the failure this bridge exists to end.
        self.list_response = json.dumps(
            [{"id": "t_old", "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "blocked", "created_by": pcd.CREATED_BY}]
        )
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)

    def blocked_board(self, n: int) -> None:
        self.board(*[(f"t_b{i}", "blocked", i) for i in range(n)])

    def test_a_backlog_under_the_cap_still_files(self):
        # The property the cap must not cost: a job that blocked twice and then
        # got fixed goes on running without anyone touching the board.
        self.blocked_board(pcd.MAX_BLOCKED - 1)
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.create_calls), 1)

    def test_a_backlog_at_the_cap_stops_filing_and_alerts(self):
        # "Blocked is not a lock" has no floor on its own: a job that blocks
        # every run files 48 cards a day, spawns 48 workers, sweeps none of
        # them, and never exits non-zero. Turn it into the page it should be.
        self.blocked_board(pcd.MAX_BLOCKED)
        rc, err = self.run_main_stderr()
        self.assertEqual(rc, 1)
        self.assertEqual(self.create_calls, [])
        self.assertIn(f"{pcd.MAX_BLOCKED} blocked card(s)", err)

    def test_the_cap_counts_only_this_jobs_blocked_cards(self):
        # Six jobs share the `platform` assignee and this creator, so a
        # fleet-wide count would let one wedged job stop the other five.
        self.list_response = json.dumps(
            [
                {"id": f"t_o{i}",
                 "title": pcd.card_title("fleet-wide-cost-analysis", "Fleet Waste Audit"),
                 "status": "blocked", "created_at": i, "created_by": pcd.CREATED_BY}
                for i in range(pcd.MAX_BLOCKED + 3)
            ]
        )
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.create_calls), 1)

    def test_an_unreadable_board_does_not_look_like_a_blocked_backlog(self):
        # Failing open means reporting no blocked cards, not an unknown number:
        # a count nobody could read is not grounds for holding the schedule.
        self.list_response = "kanban: the board is being migrated"
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.create_calls), 1)

    def test_another_jobs_open_card_does_not_block_this_one(self):
        self.list_response = json.dumps(
            [{"id": "t_other", "title": pcd.card_title("fleet-wide-cost-analysis", "Fleet Waste Audit"),
              "status": "running", "created_by": pcd.CREATED_BY}]
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
                                  "prompt": PROMPT, "enabled": True}]}),
            encoding="utf-8",
        )
        self.list_response = json.dumps(
            [{"id": "t_old",
              "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "running", "created_by": pcd.CREATED_BY}]
        )
        rc, out = self.run_main()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.create_calls, [])

    def test_cards_filed_before_the_id_existed_are_still_swept(self):
        # Without the bare-name fallback, adding the id would strand every card
        # already on the board — the exact backlog the sweep exists to prevent.
        legacy = [
            {"id": f"t_{i}", "title": "Run the Security & RBAC Posture Audit cron job",
             "status": "done", "created_at": i, "created_by": pcd.CREATED_BY}
            for i in range(pcd.KEEP_FINISHED + 2)
        ]
        self.list_response = json.dumps(legacy)
        self.run_main()
        self.assertEqual(self.archive_calls[0].split()[1:], ["t_0", "t_1"])

    def test_cards_filed_under_the_dispatch_era_title_are_still_swept(self):
        # While the card was an indirection to a platform-side cron entry the
        # title read "Run the <name> cron job [<id>]". Those cards are still on
        # live boards; the bracketed id is what carries the match across the
        # change, so the sweep does not restart from an empty history.
        old = [
            {"id": f"t_{i}",
             "title": "Run the Security & RBAC Posture Audit cron job [compliance-audit]",
             "status": "done", "created_at": i, "created_by": pcd.CREATED_BY}
            for i in range(pcd.KEEP_FINISHED + 2)
        ]
        self.list_response = json.dumps(old)
        self.run_main()
        self.assertEqual(self.archive_calls[0].split()[1:], ["t_0", "t_1"])

    def test_one_job_id_is_not_a_suffix_of_another(self):
        # `endswith("[audit]")` must not match "[compliance-audit]"; the bracket
        # is what keeps two jobs from sharing a dedup handle.
        card = {"title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
                "created_by": pcd.CREATED_BY}
        self.assertFalse(pcd._is_this_jobs_card(card, "audit", "Some Other Audit"))
        self.assertTrue(pcd._is_this_jobs_card(card, "compliance-audit", "Anything"))

    def test_a_card_a_person_asked_for_is_not_ours_to_touch(self):
        # `agents/platform/AGENTS.md` tells the Platform Agent to file
        # "Run the <name> cron job" when a user asks for a job by hand — the
        # bare-name form exactly. Claiming those would let the sweep archive a
        # human's cards, and would let one hand-run suppress the schedule.
        theirs = [
            {"id": f"t_{i}", "title": "Run the Security & RBAC Posture Audit cron job",
             "status": "done", "created_at": i, "created_by": "platform"}
            for i in range(pcd.KEEP_FINISHED + 2)
        ]
        theirs.append({"id": "t_running", "created_by": "platform", "status": "running",
                       "title": pcd.card_title("compliance-audit",
                                               "Security & RBAC Posture Audit")})
        self.list_response = json.dumps(theirs)
        self.run_main()
        self.assertEqual(self.archive_calls, [])
        self.assertEqual(len(self.create_calls), 1)


class UnknownStatusTest(DispatchHarness):
    """The board's vocabulary is not schema-constrained, so surprises must show."""

    def _card(self, status: str) -> str:
        return json.dumps(
            [{"id": "t_odd",
              "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": status, "created_at": 1, "created_by": pcd.CREATED_BY}]
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


class BoardParsingTest(DispatchHarness):
    """`--json` does not mean the buffer holds nothing but JSON.

    `run_slash` redirects stderr into the string it returns, so anything the
    board writes during the call arrives glued to the payload. Strict parsing
    made that a *permanent* failure — the noise is a property of the deployment,
    not of the tick — and both dedup layers fail open, so every tick afterwards
    would file regardless of what was running and would never sweep.
    """

    def running_card(self) -> str:
        return json.dumps(
            [{"id": "t_run",
              "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "running", "created_at": 1, "created_by": pcd.CREATED_BY}]
        )

    NOISE = {
        "a warning in front": "DeprecationWarning: datetime.utcnow() is deprecated\n{payload}",
        "a bracketed log line in front": "[ROUTER-MCP] cannot list profiles\n{payload}",
        "a diagnostic behind": "{payload}\nkanban: 1 task has no assignee",
        "noise on both sides": "sqlite3 adapter warning\n{payload}\nkanban: done",
    }

    def test_noise_around_the_listing_does_not_blind_the_dedup(self):
        for name, template in self.NOISE.items():
            with self.subTest(noise=name):
                self.calls.clear()
                self.list_response = template.format(payload=self.running_card())
                rc, _ = self.run_main()
                self.assertEqual(rc, 0)
                self.assertEqual(self.create_calls, [], "filed on top of a running card")

    def test_a_listing_that_holds_no_array_fails_open_and_says_so(self):
        # Still lenient — a board nobody can read must not retire the audits —
        # but the payload goes in the log, which is the difference between a
        # one-line fix and months of nobody knowing.
        self.list_response = "kanban: the board is being migrated"
        rc, err = self.run_main_stderr()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.create_calls), 1)
        self.assertIn("could not parse the board listing", err)
        self.assertIn("being migrated", err)

    def test_an_empty_listing_is_an_empty_board_not_a_parse_failure(self):
        self.list_response = ""
        rc, err = self.run_main_stderr()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.create_calls), 1)
        self.assertNotIn("could not parse", err)

    def test_noise_around_the_created_task_is_not_read_as_a_refusal(self):
        # The same hazard on the create, where strictness costs more than a
        # blind tick: the card exists, so the alert is false *and* the next tick
        # sees it in flight and files nothing to correct the record.
        self.create_response = 'DeprecationWarning: …\n{"id": "t_noisy"}\nkanban: tip'
        rc, err = self.run_main_stderr()
        self.assertEqual(rc, 0)
        self.assertIn("filed t_noisy", err)

    def test_the_scanner_steps_over_candidates_that_are_the_wrong_shape(self):
        self.assertIsNone(pcd._find_json("[not json] plain text", "[", list))
        self.assertEqual(pcd._find_json('[bad] [{"id": "t_1"}]', "[", list), [{"id": "t_1"}])
        self.assertEqual(pcd._find_json('{"a": 1} {"id": "t_1"}', "{", dict), {"a": 1})


class AlertingTest(DispatchHarness):
    """A non-zero exit is a page. It must fire for defects and only defects."""

    # `run_slash` does not raise. It wraps the subcommand in
    # `except SystemExit: pass` / `except Exception`, throws the return code
    # away, and returns the captured buffers — so a refusal is a *string*.
    # These are the shapes the real CLI produces: argparse rejection through
    # `run_slash`'s usage handler, and `_cmd_create`'s own guards, which print
    # to stderr and `return 2`.
    REFUSALS = (
        "⚠ /kanban usage error\nusage: /kanban create [-h] …",
        "kanban: --max-runtime: malformed duration 'zzz'",
        "error: database is locked",
    )

    def test_a_create_the_board_refuses_alerts(self):
        # The listing succeeded, so the board is up and talking and it still
        # said no. That will say no on every future tick; exiting 0 would
        # retire the audit for good with one stderr line as the only trace.
        for refusal in self.REFUSALS:
            with self.subTest(refusal=refusal):
                self.create_response = refusal
                rc, out = self.run_main()
                self.assertEqual(rc, 1)
                self.assertEqual(out, "")

    def test_the_refusal_is_logged_with_what_the_board_said(self):
        # The alert names a job; only the log says why, and "could not read a
        # task id" sent the last reader looking for a card that was never
        # created.
        self.create_response = "kanban: --max-retries must be >= 1 (got 0)"
        _, err = self.run_main_stderr()
        self.assertIn("refused the create", err)
        self.assertIn("--max-retries", err)

    def test_a_create_failure_on_an_unreachable_board_stays_quiet(self):
        # Both calls fail: the board is down, which is weather. The next tick
        # retries, and paging every 30 minutes until it clears trains people to
        # ignore the page.
        self.raises = RuntimeError("board is down")
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)

    def test_a_refusal_on_an_unreachable_board_stays_quiet(self):
        # Same weather, told a different way: the listing failed and the create
        # came back as text rather than a task. Nothing here says the board is
        # up, so nothing here justifies a page.
        self.raises = RuntimeError("board is down")
        self.create_response = "error: database is locked"
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)

    def test_a_missing_kanban_cli_alerts(self):
        # The one exception `run_slash` really can raise, since it imports
        # `hermes_cli.kanban` per call. A rename under a Hermes upgrade is
        # permanent, so failing open would retire all six audits silently.
        self.raises = ImportError("No module named 'hermes_cli'")
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")

    def test_an_in_flight_card_is_not_an_alert(self):
        self.list_response = json.dumps(
            [{"id": "t_old",
              "title": pcd.card_title("compliance-audit", "Security & RBAC Posture Audit"),
              "status": "running", "created_by": pcd.CREATED_BY}]
        )
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)

    def test_output_with_no_task_object_is_read_as_a_refusal(self):
        # It used to be read as "the card is probably there, only its id came
        # back unreadable". Under `--json` there is no such state: `_cmd_create`
        # prints one `json.dumps` of the task or nothing at all.
        self.create_response = "not a task"
        rc, _ = self.run_main()
        self.assertEqual(rc, 1)

    def test_an_id_quoted_back_inside_an_error_is_not_a_filed_card(self):
        # The tempting `t_[0-9a-f]+` fallback would match here and report the
        # duplicate's id as the card this tick filed.
        self.create_response = "error: duplicate of t_abc12345"
        rc, _ = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIsNone(pcd._parse_task_id(self.create_response))


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

    def test_a_blocked_backlog_is_still_swept_of_finished_cards(self):
        # The cap stops filing, not sweeping: the finished backlog is this
        # job's own litter either way, and leaving it would bury the blocked
        # cards the alert is sending someone to look at.
        self.board(
            *[(f"t_b{i}", "blocked", i) for i in range(pcd.MAX_BLOCKED)],
            *self.finished(pcd.KEEP_FINISHED + 2),
        )
        self.run_main()
        self.assertEqual(self.create_calls, [])
        self.assertEqual(len(self.archive_calls), 1)

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
                       "status": "done", "created_at": 1, "created_by": pcd.CREATED_BY})
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

    def test_a_refused_create_still_prunes(self):
        # The sweep is decided by the same listing that decided to file, and
        # runs before the create. A board that says no to the new card does not
        # get to keep the backlog too — and the tick still exits 1, because the
        # audit did not happen.
        self.board(*self.finished(pcd.KEEP_FINISHED + 1))
        self.create_response = "kanban: --max-runtime: malformed duration 'zzz'"
        rc, out = self.run_main()
        self.assertEqual((rc, out), (1, ""))
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
    """The card carries the job's own prompt, read off the roster at tick time."""

    def job(self, job_id="compliance-audit") -> dict:
        return next(j for j in ROSTER["jobs"] if j["id"] == job_id)

    def test_body_is_the_roster_prompt_verbatim(self):
        # Read, never restated. Editing cron/jobs.json has to be the whole of
        # editing what a watchdog does, or the copy here is the one that
        # quietly goes stale — the exact failure the two-roster design had.
        self.assertIn(PROMPT, pcd.card_body(self.job()))

    def test_the_prompt_the_card_carries_comes_from_the_shipped_roster(self):
        shipped = next(
            j
            for j in json.loads(CHAT_ROSTER.read_text(encoding="utf-8"))["jobs"]
            if j["id"] == "compliance-audit"
        )
        self.assertIn("compliance_audit_sop.md", pcd.card_body(shipped))

    def test_body_names_the_jobs_skills(self):
        # Belt to the `--skill` braces below: the card is the durable record of
        # what the job expected, and it is what a run whose preload resolved
        # nothing still has to go on.
        self.assertIn("`fleet-audit`", pcd.card_body(self.job()))

    def test_the_create_force_loads_the_jobs_skills(self):
        # Naming a skill in the body leaves loading it to the worker, and a
        # worker that declines is the one that writes findings it never
        # gathered. `--skill` is persisted on the card and reaches the spawn as
        # `--skills`, which preloads the text before the first turn — the same
        # place the cron scheduler got to by prepending it to the prompt.
        self.run_main()
        self.assertIn("--skill fleet-audit", self.create_calls[0])

    def test_a_job_with_no_skills_passes_no_skill_flag(self):
        pcd.file_card(
            "skilless",
            {"name": "Skilless Audit", "prompt": PROMPT},
            datetime(2026, 8, 7, tzinfo=timezone.utc),
        )
        self.assertNotIn("--skill", self.create_calls[0])

    def test_the_flags_and_the_body_name_the_same_skills(self):
        # One reading of the entry feeds both, so neither can drift into
        # force-loading a skill the card never mentions, or the reverse.
        job = {
            "name": "Two Skills",
            "prompt": PROMPT,
            "skills": ["fleet-audit", "  ", "github-issue-resolver"],
        }
        pcd.file_card("two", job, datetime(2026, 8, 7, tzinfo=timezone.utc))
        cmd = self.create_calls[0]
        self.assertEqual(cmd.count("--skill "), 2)
        for skill in ("fleet-audit", "github-issue-resolver"):
            self.assertIn(f"--skill {skill} ", cmd)
            self.assertIn(f"`{skill}`", pcd.card_body(job))

    def test_body_tells_the_worker_nobody_is_watching(self):
        # A cron script has no chat session, so no notify subscription is
        # written and the completion posts nowhere. The worker has to know
        # that its summary is for the board, not for a person reading along.
        self.assertIn("Nobody is watching this card in chat", pcd.card_body(self.job()))

    def test_body_requires_a_completion_that_spells_out_urls(self):
        # The card is the only durable record of what the run produced, and on
        # 2026-08-03 a summary that said "the existing ledger issue" with no
        # number was the whole report anyone got.
        body = pcd.card_body(self.job())
        self.assertIn("kanban_complete", body)
        self.assertIn("every URL in full", body)

    def test_card_is_attributed_to_cron(self):
        self.run_main()
        self.assertIn("--created-by cron", self.create_calls[0])

    def test_resolver_runtime_cap_is_under_its_own_period(self):
        # It reruns every 30 minutes. A cap at or above that lets a wedged run
        # still hold the board when the next tick arrives.
        self.assertEqual(pcd.MAX_RUNTIME["github-issue-resolver"], "25m")


class WiringTest(unittest.TestCase):
    """The shipped roster, the wrappers, and the now-empty platform roster."""

    def setUp(self):
        self.chat_jobs = json.loads(CHAT_ROSTER.read_text(encoding="utf-8"))["jobs"]
        self.dispatch_jobs = [
            j for j in self.chat_jobs if str(j.get("script", "")).startswith("dispatch_")
        ]

    def test_the_platform_roster_holds_no_jobs(self):
        # One schedule, in one file. A job left here would never fire — the
        # platform profile has no gateway and so no ticker — while reading in
        # the repository as though it did, which is how the audits went a
        # release without running and nobody noticed.
        self.assertEqual(
            json.loads(PLATFORM_ROSTER.read_text(encoding="utf-8"))["jobs"], []
        )

    def test_the_governance_jobs_are_all_here(self):
        self.assertEqual(
            sorted(j["id"] for j in self.dispatch_jobs),
            [
                "compliance-audit",
                "fleet-consistency-drift",
                "fleet-wide-cost-analysis",
                "github-issue-resolver",
                "obtainability-audit",
                "security-patch-orchestrator",
                "stockout-prevention",
            ],
        )

    def test_they_are_script_jobs_that_never_prompt_a_model(self):
        # The Chat Agent has no `terminal` and no `skills` toolset, so it
        # cannot run an audit itself however the entry is written. `no_agent`
        # is what makes the prompt a payload for the card instead of an
        # instruction to a model that could not carry it out.
        for job in self.dispatch_jobs:
            self.assertTrue(job["no_agent"], job["id"])
            self.assertTrue(job["prompt"].strip(), job["id"])

    def test_they_deliver_so_a_broken_trigger_can_alert(self):
        # A successful tick prints nothing and stays silent either way. The
        # difference `all` makes is on failure: `local` resolves to no delivery
        # target, so the watchdog alert is built and then dropped — which looks
        # exactly like the audits quietly never running again.
        for job in self.dispatch_jobs:
            self.assertEqual(job["deliver"], "all", job["id"])

    def test_every_job_points_at_a_wrapper_that_exists(self):
        for job in self.dispatch_jobs:
            wrapper = SCRIPTS_DIR / job["script"]
            self.assertTrue(wrapper.is_file(), f"{job['id']} -> {job['script']}")
            self.assertIn(
                f'main("{job["id"]}")',
                wrapper.read_text(encoding="utf-8"),
            )

    def test_every_skill_a_job_names_is_one_the_worker_can_load(self):
        # `kanban create` does not validate `--skill`, and a name that resolves
        # to nothing is reported missing rather than fatal, so a typo here
        # would ship a worker without the skill it was told it had — and only
        # the audit's empty output would ever say so.
        skills_dir = REPO / "agents/platform/skills"
        named = [(j["id"], s) for j in self.dispatch_jobs for s in j.get("skills") or []]
        self.assertTrue(named, "no job names a skill; this test guards nothing")
        for job_id, skill in named:
            with self.subTest(job=job_id, skill=skill):
                self.assertTrue((skills_dir / skill / "SKILL.md").is_file())

    def test_no_wrapper_is_orphaned(self):
        # A wrapper with no roster entry is a script nothing runs, and the next
        # reader has to work out whether that is a bug or a leftover.
        self.assertEqual(
            sorted(p.name for p in SCRIPTS_DIR.glob("dispatch_*.py")),
            sorted(j["script"] for j in self.dispatch_jobs),
        )


if __name__ == "__main__":
    unittest.main()
