import hashlib
import io
import json
import sys
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import findings_nudge as nudge


def finding(**overrides) -> dict:
    row = {
        "id": "fnd_0001",
        "severity": "critical",
        "rank_score": 288,
        "cluster": "prod",
        "namespace": "payments",
        "object": "api",
        "title": "no readinessProbe on api",
        "recommendation": {"action": "add a readinessProbe", "rationale": "traffic", "risk": "5xx"},
    }
    row.update(overrides)
    return row


class ComposeTests(unittest.TestCase):
    def test_an_empty_queue_says_so_rather_than_greeting_into_nothing(self):
        self.assertEqual(
            nudge.compose([]),
            f"{nudge.HEADING}\n\nGood morning. The findings queue is empty.",
        )

    def test_every_message_opens_with_the_heading(self):
        # The relay turn reproduces a report and answers a greeting, so a
        # message that starts with "Good morning." never reaches the user.
        for findings in ([], [finding(severity="major")], [finding()]):
            self.assertTrue(nudge.compose(findings).startswith(f"{nudge.HEADING}\n\n"))

    def test_a_queue_with_no_criticals_names_the_highest_instead(self):
        message = nudge.compose([finding(severity="major", title="no memory limit")])
        self.assertIn("No critical findings are open", message)
        self.assertIn("The highest is major: no memory limit", message)

    def test_the_top_two_criticals_are_named_and_the_third_is_only_counted(self):
        message = nudge.compose(
            [
                finding(id="a", title="first", rank_score=288),
                finding(id="b", title="second", rank_score=200),
                finding(id="c", title="third", rank_score=150),
            ]
        )
        self.assertIn("first", message)
        self.assertIn("second", message)
        self.assertNotIn("third", message)
        self.assertIn("1 more critical finding not named here", message)

    def test_exactly_two_criticals_leaves_off_the_remainder_line(self):
        message = nudge.compose([finding(id="a", title="first"), finding(id="b", title="second")])
        self.assertNotIn("not named here", message)

    def test_one_critical_reads_as_singular(self):
        message = nudge.compose([finding()])
        self.assertIn("1 critical finding is open", message)

    def test_the_queue_total_counts_every_severity_not_just_the_criticals(self):
        message = nudge.compose([finding(), finding(id="b", severity="minor")])
        self.assertIn("1 critical finding is open, 2 in the queue", message)

    def test_a_cluster_scoped_finding_names_its_cluster_once(self):
        message = nudge.compose([finding(namespace="", object="prod", title="WI disabled")])
        self.assertIn("\n   prod\n", message)
        self.assertNotIn("prod/prod", message)

    def test_a_namespaced_finding_keeps_all_three_segments(self):
        message = nudge.compose([finding()])
        self.assertIn("prod/payments/api", message)

    def test_a_finding_with_no_recommended_action_prints_without_a_blank_line(self):
        message = nudge.compose([finding(recommendation={})])
        self.assertIn("no readinessProbe on api", message)
        self.assertNotIn("\n   \n", message)
        self.assertFalse(message.endswith("\n   "))


class ProviderManagedTests(unittest.TestCase):
    """§4.4: scored and on the list, but never named in a nudge."""

    def test_a_provider_managed_critical_is_not_named(self):
        message = nudge.compose(
            [
                finding(id="a", title="kube-system has no limits", provider_managed=True),
                finding(id="b", title="payments api has no probe"),
            ]
        )
        self.assertNotIn("kube-system has no limits", message)
        self.assertIn("payments api has no probe", message)
        self.assertIn("1 critical finding is open, 2 in the queue", message)
        self.assertIn("1 provider-managed item is also on the list", message)

    def test_a_provider_managed_row_is_not_the_highest_when_nothing_is_critical(self):
        message = nudge.compose(
            [
                finding(id="a", severity="major", title="gke-managed thing", provider_managed=True),
                finding(id="b", severity="minor", title="yours"),
            ]
        )
        self.assertIn("The highest is minor: yours", message)
        self.assertNotIn("gke-managed thing", message)

    def test_a_queue_of_nothing_but_provider_managed_rows_says_so(self):
        message = nudge.compose([finding(id="a", provider_managed=True)])
        self.assertIn("Nothing on the queue is yours to act on", message)


class NudgeHarness(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        patch_out = unittest.mock.patch.object(sys, "stdout", self.out)
        patch_err = unittest.mock.patch.object(sys, "stderr", self.err)
        patch_out.start()
        patch_err.start()
        self.addCleanup(patch_out.stop)
        self.addCleanup(patch_err.stop)

    def run_with(self, ranked, surfaced_error=None, last_hash=None, publication_error=None):
        """Drive `main` against a stubbed queue, recording every request made."""
        calls = []
        self.published = []

        def fake_request(endpoint, path, body=None, method=""):
            calls.append(path)
            if path == "/v1/findings/ranked":
                return {"findings": ranked}
            if path == f"/v1/findings/publication/{nudge.PUBLISHER}":
                if publication_error:
                    raise publication_error
                if method == "PUT":
                    self.published.append(body)
                    return body
                if last_hash is None:
                    raise urllib.error.HTTPError(path, 404, "not found", None, None)
                return {"content_hash": last_hash}
            if surfaced_error:
                raise surfaced_error
            return {}

        with unittest.mock.patch.object(nudge, "_request", side_effect=fake_request):
            code = nudge.main([])
        return code, calls


class MainTests(NudgeHarness):
    def test_only_the_criticals_the_message_named_are_marked_surfaced(self):
        code, calls = self.run_with(
            [
                finding(id="a"),
                finding(id="b"),
                finding(id="c"),
                finding(id="d", severity="minor"),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [
                "/v1/findings/ranked",
                f"/v1/findings/publication/{nudge.PUBLISHER}",
                f"/v1/findings/publication/{nudge.PUBLISHER}",
                "/v1/findings/a/surfaced",
                "/v1/findings/b/surfaced",
            ],
        )

    def test_a_provider_managed_critical_is_not_marked_surfaced(self):
        _, calls = self.run_with(
            [finding(id="managed", provider_managed=True), finding(id="mine")]
        )
        self.assertIn("/v1/findings/mine/surfaced", calls)
        self.assertNotIn("/v1/findings/managed/surfaced", calls)

    def test_a_failed_surfaced_call_costs_the_bookkeeping_not_the_message(self):
        code, _ = self.run_with([finding(id="a")], surfaced_error=urllib.error.URLError("refused"))
        self.assertEqual(code, 0)
        self.assertIn("no readinessProbe on api", self.out.getvalue())
        self.assertIn("could not mark a surfaced", self.err.getvalue())

    def test_an_unreachable_queue_exits_non_zero_with_nothing_on_stdout(self):
        with unittest.mock.patch.object(nudge, "_request", side_effect=urllib.error.URLError("refused")):
            code = nudge.main([])
        self.assertEqual(code, 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("could not read the queue", self.err.getvalue())


class ChangeGateTests(NudgeHarness):
    """§7.2: post only when the message changed, so an empty queue is not a daily greeting."""

    def _digest(self, ranked):
        return hashlib.sha256(nudge.compose(ranked).encode("utf-8")).hexdigest()

    def test_an_unchanged_empty_queue_says_nothing_at_all(self):
        code, calls = self.run_with([], last_hash=self._digest([]))
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertNotIn(f"/v1/findings/publication/{nudge.PUBLISHER}", calls[2:])

    def test_an_unchanged_queue_of_findings_says_nothing_either(self):
        ranked = [finding(id="a")]
        code, _ = self.run_with(ranked, last_hash=self._digest(ranked))
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), "")

    def test_a_first_run_has_no_recorded_hash_and_posts(self):
        code, _ = self.run_with([])
        self.assertEqual(code, 0)
        self.assertIn("The findings queue is empty", self.out.getvalue())

    def test_a_changed_queue_posts_and_records_the_new_hash(self):
        ranked = [finding(id="a")]
        code, _ = self.run_with(ranked, last_hash=self._digest([]))
        self.assertEqual(code, 0)
        self.assertIn("no readinessProbe on api", self.out.getvalue())
        self.assertEqual(
            self.published,
            [{"target_kind": "chat", "content_hash": self._digest(ranked)}],
        )

    def test_a_queue_that_cannot_be_read_for_its_hash_posts_anyway(self):
        code, _ = self.run_with([], publication_error=urllib.error.URLError("refused"))
        self.assertEqual(code, 0)
        self.assertIn("The findings queue is empty", self.out.getvalue())
        self.assertIn("could not read the last posted hash", self.err.getvalue())

    def test_a_hash_that_cannot_be_recorded_costs_the_gate_not_the_message(self):
        recorded = []

        def fake_request(endpoint, path, body=None, method=""):
            if path == "/v1/findings/ranked":
                return {"findings": []}
            if method == "PUT":
                raise urllib.error.URLError("refused")
            recorded.append(path)
            raise urllib.error.HTTPError(path, 404, "not found", None, None)

        with unittest.mock.patch.object(nudge, "_request", side_effect=fake_request):
            code = nudge.main([])
        self.assertEqual(code, 0)
        self.assertIn("The findings queue is empty", self.out.getvalue())
        self.assertIn("could not record the posted hash", self.err.getvalue())


class RosterTests(unittest.TestCase):
    """The job entry is the whole deployment of this script, so it earns a test."""

    def test_the_roster_runs_this_script_after_the_last_daily_audit(self):
        roster = json.loads(
            (Path(__file__).parent.parent / "cron" / "jobs.json").read_text(encoding="utf-8")
        )
        jobs = {job["id"]: job for job in roster["jobs"]}
        job = jobs["findings-morning-nudge"]

        self.assertEqual(job["script"], "findings_nudge.py")
        self.assertTrue(job["no_agent"])
        self.assertTrue(job["enabled"])
        # Anything but an audible target writes the run to `last_output` and
        # delivers nowhere, so a broken nudge would read as a quiet morning.
        self.assertIn(job["deliver"], ("chat", "all"))
        self.assertEqual(job["schedule"]["expr"], job["schedule"]["display"])

        def hour(job_id):
            minute, hour_field = jobs[job_id]["schedule"]["expr"].split()[:2]
            return int(hour_field) * 60 + int(minute)

        # The daily audits are themselves a source of findings; a nudge that ran
        # first would publish a list a day behind its own inputs.
        latest_daily = max(
            hour(job_id)
            for job_id, other in jobs.items()
            if other["schedule"]["expr"].split()[4] == "*" and not other.get("no_agent")
        )
        self.assertGreater(hour("findings-morning-nudge"), latest_daily)


if __name__ == "__main__":
    unittest.main()
