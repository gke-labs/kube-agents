"""Unit tests for github_scan_gate.py, the ``github-repo-watcher`` dispatcher.

Run: python3 -m unittest agents/platform/scripts/test_github_scan_gate.py

Three properties carry the weight here.

The first is that **an idle tick is silent on stdout**. Stdout is the delivery
channel: the scheduler posts whatever this job prints, so a stray line turns a
ten-minute poll into 144 chat messages a day. That is the failure the whole
gate exists to avoid, so it is asserted directly rather than inferred from "no
card was filed".

The second is that **silence and fault stay apart** — the same distinction
``test_resolver.py`` protects one layer down. ``NO_ISSUES`` and
``NOT_CONFIGURED`` are supported states; a resolver that cannot run is not, and
must reach the room. A gate that flattened the two would make a broken watcher
indistinguishable from a quiet repository.

The third is **sweep isolation**. Consolidating two cron jobs into one script
gave up the isolation two jobs had for free, and the ``try`` per sweep is what
buys it back. If a raising sweep could abort the loop, the consolidation would
have traded a token saving for a single point of failure.
"""

import importlib
import inspect
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# Import the module under test from this directory.
sys.path.insert(0, str(Path(__file__).parent.absolute()))
gate = importlib.import_module("github_scan_gate")


def _completed(stdout: str, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["python3", "resolver.py", "poll"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class SweepRegistryTest(unittest.TestCase):
    def test_every_registered_sweep_is_runnable(self):
        """`SWEEP_ORDER` and `SWEEPS` cannot drift apart.

        They used to be two hand-written lists. A sweep added to only the
        registry never runs, and `GITHUB_WATCHER_SWEEPS` then reports the
        operator's correct name as an unknown sweep — pointing them at their
        own env var rather than at the missing line. Deriving one from the
        other makes that unrepresentable; this asserts the derivation stays.
        """
        self.assertEqual(set(gate.SWEEP_ORDER), set(gate.SWEEPS))
        self.assertEqual(len(gate.SWEEP_ORDER), len(gate.SWEEPS))

    def test_a_sweep_receives_the_dry_run_flag(self):
        """`--dry-run` has to reach the sweep, not just the card filing.

        The issues sweep is the one that cannot honour it — `resolver.py poll`
        writes its stale-label sweep to GitHub either way — and it says so on
        stderr. Calling the sweep with no argument silently swallowed that
        caveat, leaving `--dry-run` looking read-only when it is not.
        """
        seen = []
        with mock.patch.dict(
            gate.SWEEPS, {"issues": lambda dry_run=False: seen.append(dry_run) or gate.SweepResult()},
            clear=True,
        ), mock.patch.object(gate, "SWEEP_ORDER", ("issues",)):
            gate.main(["--dry-run"])
            gate.main([])
        self.assertEqual(seen, [True, False])

    def test_every_registered_sweep_accepts_the_flag(self):
        """Against the real callables, which the stub above cannot check.

        `main` passes `dry_run` positionally. A sweep declared without the
        parameter raises `TypeError`, and the deliberately broad `except` turns
        that into a `⚠️` line — so the job would announce itself broken every
        ten minutes and never poll, while every test that drives `SWEEPS`
        through a stub carried on passing.
        """
        for name, sweep in gate.SWEEPS.items():
            with self.subTest(sweep=name):
                inspect.signature(sweep).bind(False)


class SelectedSweepsTest(unittest.TestCase):
    def test_unset_runs_every_sweep(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(gate.SWEEPS_ENV, None)
            selected, warnings = gate.selected_sweeps()
        self.assertEqual(selected, gate.SWEEP_ORDER)
        self.assertEqual(warnings, [])

    def test_subset_is_honoured(self):
        with mock.patch.dict("os.environ", {gate.SWEEPS_ENV: "issues"}):
            selected, warnings = gate.selected_sweeps()
        self.assertEqual(selected, ("issues",))
        self.assertEqual(warnings, [])

    def test_misspelled_name_is_reported_not_ignored(self):
        """A typo must not read as "disable everything" in silence.

        ``GITHUB_WATCHER_SWEEPS=issue`` selects nothing. Accepting that quietly
        would stop the watcher permanently with no signal anywhere — the exact
        outcome this job was written to prevent.
        """
        with mock.patch.dict("os.environ", {gate.SWEEPS_ENV: "issue"}):
            selected, warnings = gate.selected_sweeps()
        self.assertEqual(selected, ())
        self.assertTrue(any("unknown" in w for w in warnings))
        self.assertTrue(any("doing nothing" in w for w in warnings))

    def test_order_follows_sweep_order_not_the_env(self):
        """The env selects; it does not reorder.

        Sweep order is a property of the script, so that the cheapest sweep can
        be placed first later without an operator's env var overriding it.
        """
        with mock.patch.dict("os.environ", {gate.SWEEPS_ENV: "issues,issues"}):
            selected, _ = gate.selected_sweeps()
        self.assertEqual(selected, ("issues",))


class IssuesSweepTest(unittest.TestCase):
    def _poll(self, payload):
        return mock.patch.object(
            gate, "run_resolver_poll", return_value=payload
        )

    def test_no_issues_is_silence(self):
        with self._poll({"status": "NO_ISSUES", "repository": "o/r"}):
            result = gate.sweep_issues()
        self.assertEqual(result.cards, [])
        self.assertEqual(result.warnings, [])

    def test_not_configured_is_silence_not_a_fault(self):
        """An install with no target repository is supported, not broken."""
        with self._poll({"status": "NOT_CONFIGURED"}):
            result = gate.sweep_issues()
        self.assertEqual(result.cards, [])
        self.assertEqual(result.warnings, [])

    def test_error_reaches_the_room(self):
        with self._poll({"status": "ERROR", "reason": "GITHUB_AUTH_NOT_CONFIGURED"}):
            result = gate.sweep_issues()
        self.assertEqual(result.cards, [])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("GITHUB_AUTH_NOT_CONFIGURED", result.warnings[0])

    def test_error_value_is_carried_through(self):
        """``GIT_REPO_UNPARSEABLE`` is only actionable with the offending value."""
        with self._poll(
            {"status": "ERROR", "reason": "GIT_REPO_UNPARSEABLE", "value": "not a url"}
        ):
            result = gate.sweep_issues()
        self.assertIn("not a url", result.warnings[0])

    def test_unrecognised_status_is_a_warning_not_silence(self):
        """A resolver that grows a new status must not be read as "nothing to do"."""
        with self._poll({"status": "SOMETHING_NEW"}):
            result = gate.sweep_issues()
        self.assertEqual(result.cards, [])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("SOMETHING_NEW", result.warnings[0])

    def test_found_produces_one_card(self):
        with self._poll(
            {
                "status": "FOUND",
                "repository": "gke-labs/kube-agents",
                "issue_number": 42,
                "title": "Unhealthy Config Controller",
            }
        ):
            result = gate.sweep_issues()
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.cards), 1)
        card = result.cards[0]
        self.assertIn("42", card.title)
        self.assertIn("#42", card.body)
        self.assertIn("github-issue-resolver", card.body)

    def test_idempotency_key_is_scoped_to_the_repository(self):
        """#12 on one repo is not #12 on another.

        A deployment can be repointed, and the board dedupes on this key alone,
        so a bare issue number would suppress a real card on the new repo.
        """
        with self._poll(
            {"status": "FOUND", "repository": "a/one", "issue_number": 12, "title": "t"}
        ):
            first = gate.sweep_issues().cards[0]
        with self._poll(
            {"status": "FOUND", "repository": "b/two", "issue_number": 12, "title": "t"}
        ):
            second = gate.sweep_issues().cards[0]
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertNotIn("/", first.idempotency_key)


class CardBucketTest(unittest.TestCase):
    """The idempotency key must expire, or a stuck issue wedges the watcher.

    The board matches a repeat key against non-archived rows whatever their
    state, so a *finished* card answers it forever and nothing here archives
    cards. A worker that ends its turn before the skill's Step 2 — which Step 1
    prescribes on an ``ERROR`` status — leaves the issue with no ``status:``
    label, so the poll keeps returning it and the key keeps suppressing it. The
    poll returns only the lowest-numbered unaddressed issue, so that also hides
    every higher-numbered one.
    """

    PAYLOAD = {
        "status": "FOUND",
        "repository": "o/r",
        "issue_number": 7,
        "title": "t",
    }

    def test_the_same_hour_does_not_refile(self):
        early = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)
        late = datetime(2026, 8, 17, 14, 59, tzinfo=timezone.utc)
        self.assertEqual(
            gate._issue_card(self.PAYLOAD, now=early).idempotency_key,
            gate._issue_card(self.PAYLOAD, now=late).idempotency_key,
        )

    def test_the_next_hour_refiles(self):
        before = datetime(2026, 8, 17, 14, 59, tzinfo=timezone.utc)
        after = datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)
        self.assertNotEqual(
            gate._issue_card(self.PAYLOAD, now=before).idempotency_key,
            gate._issue_card(self.PAYLOAD, now=after).idempotency_key,
        )

    def test_the_repository_and_number_still_scope_the_key(self):
        """Bucketing must not have replaced the other two scopes."""
        now = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)
        key = gate._issue_card(self.PAYLOAD, now=now).idempotency_key
        other_repo = gate._issue_card(
            {**self.PAYLOAD, "repository": "other/repo"}, now=now
        ).idempotency_key
        other_number = gate._issue_card(
            {**self.PAYLOAD, "issue_number": 8}, now=now
        ).idempotency_key
        self.assertNotEqual(key, other_repo)
        self.assertNotEqual(key, other_number)

    def test_the_default_clock_is_utc_not_local(self):
        """A local-time bucket would shift under the pod's TZ."""
        card = gate._issue_card(self.PAYLOAD)
        expected = datetime.now(timezone.utc).strftime(gate.CARD_BUCKET_FORMAT)
        self.assertTrue(card.idempotency_key.endswith(expected))


class RunResolverPollTest(unittest.TestCase):
    def test_missing_resolver_raises(self):
        """Better a loud sweep failure than a silent "no issues"."""
        with mock.patch.object(gate, "_resolver_path", return_value=Path("/nope.py")):
            with self.assertRaises(FileNotFoundError):
                gate.run_resolver_poll()

    def test_json_on_stdout_is_returned_even_when_the_exit_code_is_nonzero(self):
        """The resolver reports faults as JSON and may still exit non-zero.

        Treating a non-zero exit as fatal here would discard the reason code the
        operator actually needs.
        """
        payload = {"status": "ERROR", "reason": "REPO_UNREACHABLE"}
        with mock.patch.object(gate, "_resolver_path", return_value=Path(__file__)), \
             mock.patch.object(
                 subprocess, "run", return_value=_completed(json.dumps(payload), 1)
             ):
            self.assertEqual(gate.run_resolver_poll(), payload)

    def test_empty_output_raises(self):
        with mock.patch.object(gate, "_resolver_path", return_value=Path(__file__)), \
             mock.patch.object(
                 subprocess, "run", return_value=_completed("", 1, "boom")
             ):
            with self.assertRaises(RuntimeError) as ctx:
                gate.run_resolver_poll()
        self.assertIn("boom", str(ctx.exception))

    def test_non_json_output_raises(self):
        with mock.patch.object(gate, "_resolver_path", return_value=Path(__file__)), \
             mock.patch.object(
                 subprocess, "run", return_value=_completed("Traceback ...")
             ):
            with self.assertRaises(RuntimeError):
                gate.run_resolver_poll()


class MainTest(unittest.TestCase):
    """The dispatcher: stdout discipline, card filing, and sweep isolation."""

    def _run(self, sweeps, argv=None, env=None):
        """Run main() with a substituted SWEEPS registry, capturing stdout."""
        buf = io.StringIO()
        filed = []
        with mock.patch.dict(gate.SWEEPS, sweeps, clear=True), \
             mock.patch.object(gate, "SWEEP_ORDER", tuple(sweeps)), \
             mock.patch.object(gate, "file_card", side_effect=lambda c: filed.append(c)), \
             mock.patch.dict("os.environ", env or {}, clear=False), \
             redirect_stdout(buf):
            import os

            if not env:
                os.environ.pop(gate.SWEEPS_ENV, None)
            rc = gate.main(argv or [])
        return rc, buf.getvalue(), filed

    def test_idle_tick_prints_nothing(self):
        """The property the whole job exists for: silence costs nothing."""
        rc, out, filed = self._run({"issues": lambda dry_run=False: gate.SweepResult()})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(filed, [])

    def test_found_files_a_card_and_still_prints_nothing(self):
        """Work is handed to a worker, not announced. The card is the message."""
        card = gate.Card(title="t", body="b", idempotency_key="k")
        rc, out, filed = self._run(
            {"issues": lambda dry_run=False: gate.SweepResult(cards=[card])}
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(filed, [card])

    def test_warnings_reach_stdout(self):
        rc, out, _ = self._run(
            {"issues": lambda dry_run=False: gate.SweepResult(warnings=["⚠️ broken"])}
        )
        self.assertEqual(rc, 0)
        self.assertIn("⚠️ broken", out)

    def test_a_raising_sweep_does_not_stop_its_sibling(self):
        """Sweep isolation — what two separate cron jobs used to give for free."""

        def boom(dry_run=False):
            raise RuntimeError("kaboom")

        card = gate.Card(title="t", body="b", idempotency_key="k")
        rc, out, filed = self._run(
            {"broken": boom, "working": lambda dry_run=False: gate.SweepResult(cards=[card])}
        )
        self.assertEqual(rc, 0)
        self.assertEqual(filed, [card])
        self.assertIn("kaboom", out)
        self.assertIn("`broken` sweep failed", out)

    def test_a_raising_sweep_is_reported_not_swallowed(self):
        def boom(dry_run=False):
            raise RuntimeError("kaboom")

        rc, out, filed = self._run({"broken": boom})
        self.assertEqual(rc, 0)
        self.assertEqual(filed, [])
        self.assertNotEqual(out, "")

    def test_env_can_disable_one_sweep(self):
        """The per-job `enabled: false` an operator lost when the jobs merged."""
        wanted = gate.Card(title="wanted", body="b", idempotency_key="k1")
        unwanted = gate.Card(title="unwanted", body="b", idempotency_key="k2")
        rc, out, filed = self._run(
            {
                "issues": lambda dry_run=False: gate.SweepResult(cards=[wanted]),
                "pr_comments": lambda dry_run=False: gate.SweepResult(cards=[unwanted]),
            },
            env={gate.SWEEPS_ENV: "issues"},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(filed, [wanted])
        self.assertEqual(out, "")

    def test_dry_run_files_nothing(self):
        card = gate.Card(title="t", body="b", idempotency_key="k")
        rc, out, filed = self._run(
            {"issues": lambda dry_run=False: gate.SweepResult(cards=[card])}, argv=["--dry-run"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(filed, [])
        self.assertEqual(out, "")


class ParseTaskIdTest(unittest.TestCase):
    def test_json_object_embedded_in_other_output(self):
        self.assertEqual(
            gate._parse_task_id('warning: something\n{"id": "T-7", "title": "x"}'), "T-7"
        )

    def test_falls_back_to_the_human_line(self):
        self.assertEqual(gate._parse_task_id("Created T-9  (backlog)"), "T-9")

    def test_unreadable_response_is_none(self):
        self.assertIsNone(gate._parse_task_id("board is down"))


if __name__ == "__main__":
    unittest.main()
