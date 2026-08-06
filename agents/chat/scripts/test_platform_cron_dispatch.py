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
        self.raises: Exception | None = None

        self._real_run_slash = pcd._run_slash
        pcd._run_slash = self._fake_run_slash

    def tearDown(self):
        pcd._run_slash = self._real_run_slash
        self._tmp.cleanup()

    def _fake_run_slash(self, cmd: str) -> str:
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        return self.list_response if cmd.startswith("list") else self.create_response

    def run_main(self, job_id="compliance-audit") -> tuple[int, str]:
        """Run main, returning (exit code, anything it printed to stdout)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = pcd.main(job_id, roster_paths=(self.roster_path,))
        return rc, buf.getvalue()

    @property
    def create_calls(self) -> list[str]:
        return [c for c in self.calls if c.startswith("create")]


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
            [{"id": "t_old", "title": pcd.card_title("Security & RBAC Posture Audit"),
              "status": "running"}]
        )
        rc, out = self.run_main()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.create_calls, [])

    def test_a_finished_card_does_not_block_the_next_tick(self):
        self.list_response = json.dumps(
            [{"id": "t_old", "title": pcd.card_title("Security & RBAC Posture Audit"),
              "status": "done"}]
        )
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)

    def test_a_blocked_card_does_not_block_the_next_tick(self):
        # Deliberate: a blocked card waits on a human, and treating it as
        # in-flight would let one bad run switch the audit off indefinitely —
        # silently, which is exactly the failure this bridge exists to end.
        self.list_response = json.dumps(
            [{"id": "t_old", "title": pcd.card_title("Security & RBAC Posture Audit"),
              "status": "blocked"}]
        )
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)

    def test_another_jobs_open_card_does_not_block_this_one(self):
        self.list_response = json.dumps(
            [{"id": "t_other", "title": "Run the Fleet Waste Audit cron job",
              "status": "running"}]
        )
        self.run_main()
        self.assertEqual(len(self.create_calls), 1)

    def test_an_unreadable_board_fails_open(self):
        self.list_response = "not json"
        self.run_main()
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
