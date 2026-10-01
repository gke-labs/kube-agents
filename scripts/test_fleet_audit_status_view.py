"""Tests for the fleet-audit status view.

The contract under test is the read side of
docs/designs/fleet-audit-report-store.md: the one-projection read
(`kubectl exec -i … -- python3 -` with report_status.py on stdin), the
`--json`/`--file` round trip that makes the view reproducible off-cluster, the
five flags (NO STORE, DIED, UNRECORDED, NEVER, STALE), and the exit codes that keep "I
could not look" from rendering as "nothing is wrong".

The subprocess boundary is stubbed everywhere; no test reaches a cluster.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory, mkdtemp
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The producer's side, for the round trip: the writer and the projection the
# view streams into the pod.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "agents/platform/skills/fleet-audit/scripts")
)

import audit_report  # noqa: E402
import fleet_audit_status_view as view  # noqa: E402
import report_status  # noqa: E402
import terminal_table  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
LOS_ANGELES = ZoneInfo("America/Los_Angeles")

#: A roster that parses and holds no fleet-audit job. Most tests here are about
#: something other than the roster and want the ENABLED/SCHEDULE columns out of
#: the way; they point at this rather than at a path that does not exist,
#: because a roster that cannot be read is a finding in its own right — it
#: disarms NEVER and STALE — and no longer a quiet way to ask for no roster.
_ROSTER_DIR = TemporaryDirectory()
NO_ROSTER = str(Path(_ROSTER_DIR.name) / "empty-jobs.json")


def setUpModule():
    Path(NO_ROSTER).write_text('{"jobs": []}', encoding="utf-8")


def tearDownModule():
    _ROSTER_DIR.cleanup()


def latest(**overrides):
    """One projected `latest` — report_status.py's envelope minus `document`,
    plus the derived counts."""
    base = {
        "audit_id": "compliance-audit",
        "repo": "acme/fleet",
        "finished_at": "2026-08-26T06:31:12+00:00",
        "status": "UPDATED",
        "issue_number": 12,
        "issue_url": "https://github.com/acme/fleet/issues/12",
        "partial": False,
        "coverage_gaps": [],
        "prs_opened": ["https://github.com/acme/fleet/pull/9"],
        "prs_closed": [],
        "silent_ok": None,
        "id_scheme": "sha1-12",
        # Always on a projected `latest` (report_status.LATEST_KEYS).
        "delta_known": True,
        "new": 3,
        "resolved": 1,
        "current": 57,
        "findings": 57,
        "critical": 2,
        "clusters": 4,
        "skipped": 0,
    }
    base.update(overrides)
    return base


def stream(liveness=None, last=None, started=None, error=None, runs=(), repo="acme/fleet"):
    """One stream as `report_status.project` shapes it: the lease, and the one
    repository's store when it has a run or a read error to carry. Without a
    lease, an error makes the liveness `error`, as `report_status.liveness`
    decides it."""
    liveness = liveness or ("error" if error else "completed")
    repos = {}
    if last is not None or runs or error:
        repos[repo] = {"latest": last, "runs": list(runs), "error": error}
    return {
        "started": started,
        "repos": repos,
        "liveness": liveness,
        "error": error,
    }


def projection(streams=None, root_exists=True, lease_error=None, root_error=None):
    return {
        "root": "/opt/data/fleet-audit/reports",
        "root_exists": root_exists,
        "root_error": root_error,
        "lease_error": lease_error,
        "generated_at": NOW.isoformat(),
        "ttl_s": 7200,
        "streams": streams or {},
    }


def started(age_s=300.0):
    epoch = NOW.timestamp() - age_s
    return {
        "started_at": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        "age_s": age_s,
    }


class FakeKubectl:
    """The subprocess boundary, recorded and canned. `get pods` answers with
    one `<pod> <containers>` line per pod, `config` with the kubeconfig
    probes, `exec` with the projection. `containers` is one string for every
    pod, or a mapping from pod to its own."""

    def __init__(
        self,
        pods=("agent-0",),
        containers="shell",
        get_rc=0,
        get_stderr="",
        exec_rc=0,
        exec_stdout=None,
        exec_stderr="",
        contexts=(),
        current="hub",
    ):
        self.pods = list(pods)
        self.containers = containers
        self.get_rc = get_rc
        self.get_stderr = get_stderr
        self.exec_rc = exec_rc
        self.exec_stdout = (
            json.dumps(projection()) if exec_stdout is None else exec_stdout
        )
        self.exec_stderr = exec_stderr
        self.contexts = list(contexts)
        self.current = current
        self.calls = []

    def __call__(self, cmd, capture_output=False, text=False, input=None, timeout=None):
        self.calls.append({"cmd": list(cmd), "input": input, "timeout": timeout})
        if "config" in cmd:
            answer = self.current if "current-context" in cmd else "\n".join(self.contexts)
            return CompletedProcess(cmd, 0, answer, "")
        if "get" in cmd:
            listing = "".join(
                f"{pod} {self.containers[pod] if isinstance(self.containers, dict) else self.containers}\n"
                for pod in self.pods
            )
            return CompletedProcess(cmd, self.get_rc, listing, self.get_stderr)
        return CompletedProcess(cmd, self.exec_rc, self.exec_stdout, self.exec_stderr)

    @property
    def exec_call(self):
        return next(c for c in self.calls if "exec" in c["cmd"])

    def cmds(self, needle):
        return [c["cmd"] for c in self.calls if needle in c["cmd"]]


def run_main(argv, fake=None):
    """main() with the subprocess boundary stubbed. Returns (rc, out, err)."""
    fake = fake or FakeKubectl()
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(view.subprocess, "run", fake):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = view.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestScrub(unittest.TestCase):
    def test_control_characters_never_reach_the_terminal(self):
        self.assertEqual(view.scrub("a\x1b]8;;evil\x07b"), "a�]8;;evil�b")

    def test_every_c1_control_is_scrubbed(self):
        # NEL, IND and RI move the cursor; SOS, PM and APC swallow text up to
        # the next ST, which every hyperlink ends with.
        c1 = "".join(chr(code) for code in range(0x80, 0xA0))
        self.assertEqual(view.scrub(c1), "\ufffd" * len(c1))

    def test_none_becomes_empty(self):
        self.assertEqual(view.scrub(None), "")


class TestAsProjection(unittest.TestCase):
    def test_a_valid_document_passes(self):
        self.assertEqual(view.as_projection(json.dumps(projection()), "x")["ttl_s"], 7200)

    def test_non_json_is_an_exit_2_error_not_an_empty_fleet(self):
        with self.assertRaises(view.ProjectionError) as caught:
            view.as_projection("Traceback (most recent call last):", "pod")
        self.assertIn("not JSON", str(caught.exception))

    def test_json_without_streams_is_rejected(self):
        with self.assertRaises(view.ProjectionError):
            view.as_projection('{"root": "/x"}', "pod")


class TestNextFire(unittest.TestCase):
    def test_daily(self):
        after = datetime(2026, 8, 26, 6, 31, tzinfo=timezone.utc)
        fire = view.next_fire("20 6 * * *", after)
        self.assertEqual(fire, datetime(2026, 8, 27, 6, 20, tzinfo=timezone.utc))

    def test_weekly_monday(self):
        # 2026-08-26 is a Wednesday; cron dow 1 is Monday.
        after = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        fire = view.next_fire("20 7 * * 1", after)
        self.assertEqual(fire, datetime(2026, 8, 31, 7, 20, tzinfo=timezone.utc))

    def test_the_fields_are_read_in_the_schedule_zone(self):
        # 07:00 in Los Angeles; the 06:20 there is 13:20 UTC the next day, not
        # the 06:20 UTC a zone-blind read would expect.
        after = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
        fire = view.next_fire("20 6 * * *", after, LOS_ANGELES)
        self.assertEqual(fire, datetime(2026, 8, 27, 13, 20, tzinfo=timezone.utc))
        self.assertEqual(fire.tzinfo, timezone.utc)

    def test_a_fire_keeps_its_wall_clock_hour_across_dst(self):
        # Los Angeles leaves daylight time at 02:00 on 2026-11-01, so the
        # 06:20 there moves from 13:20 UTC to 14:20 UTC.
        after = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)
        fire = view.next_fire("20 6 * * *", after, LOS_ANGELES)
        self.assertEqual(fire, datetime(2026, 11, 1, 14, 20, tzinfo=timezone.utc))

    def test_anything_fancier_abstains(self):
        self.assertIsNone(view.next_fire("*/5 * * * *", NOW))
        self.assertIsNone(view.next_fire("20 6 1 * *", NOW))
        self.assertIsNone(view.next_fire("garbage", NOW))

    def test_an_out_of_range_field_abstains_rather_than_raising(self):
        # These parse as ints and then blow up in `.replace()`. Uncaught, one
        # mistyped roster entry took down every row on its way through
        # `render` — a worse outcome than the schedule column it belongs to.
        for expr in ("99 3 * * *", "20 25 * * *", "-1 6 * * *"):
            with self.subTest(expr=expr):
                self.assertIsNone(view.next_fire(expr, NOW))

    def test_one_bad_entry_does_not_take_down_the_table(self):
        roster = {
            "compliance-audit": {"enabled": True, "expr": "99 3 * * *"},
            "ai-security-audit": {"enabled": True, "expr": "20 6 * * *"},
        }
        text = view.render(
            projection({"compliance-audit": stream(last=latest())}),
            roster,
            NOW,
            "/x/jobs.json",
            "file",
        )
        self.assertIn("compliance-audit", text)
        self.assertIn("ai-security-audit", text)


class TestFlags(unittest.TestCase):
    JOB = {"enabled": True, "expr": "20 6 * * *"}

    def flags(self, stream_doc, job=None, root_exists=True):
        # Through `stream_rows`, as `render` does: a row reads one
        # repository's `latest`, not the projection's per-stream shape.
        (_, _, source), = view.stream_rows({"cost-audit": stream_doc}, {})
        return view.flags_for(source, job or self.JOB, NOW, root_exists)

    def test_a_recent_run_carries_no_flag(self):
        recent = latest(finished_at=(NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(self.flags(stream(last=recent)), [])

    def test_a_missing_store_is_no_store(self):
        self.assertEqual(self.flags(stream(), root_exists=False), ["NO STORE"])

    def test_an_unreadable_stream_is_no_store(self):
        doc = stream(liveness="error", error="latest.json: not a JSON object")
        self.assertEqual(self.flags(doc), ["NO STORE"])

    def test_a_stream_level_error_reaches_a_row_whose_repository_read_fine(self):
        # An unreadable lease or a stray directory belongs to no repository,
        # so the row must take it from the stream rather than read clean.
        doc = stream(last=latest(finished_at=(NOW - timedelta(hours=2)).isoformat()))
        doc.update(liveness="error", error="the in-flight note: denied")
        self.assertEqual(self.flags(doc), ["NO STORE"])

    def test_a_row_from_the_ring_is_flagged_unrecorded(self):
        recent = latest(finished_at=(NOW - timedelta(hours=2)).isoformat())
        doc = stream(last=recent)
        doc["repos"]["acme/fleet"]["latest_missing"] = True
        self.assertEqual(self.flags(doc), ["UNRECORDED"])

    def test_a_sibling_repositorys_error_reaches_the_healthy_row(self):
        recent = latest(finished_at=(NOW - timedelta(hours=2)).isoformat())
        doc = stream(last=recent)
        doc["repos"]["acme/other"] = {"latest": None, "runs": [], "error": "latest.json: bad"}
        doc.update(liveness="error", error="acme/other: latest.json: bad")
        rows = view.stream_rows({"cost-audit": doc}, {})
        self.assertTrue(all(source["error"] for _, _, source in rows))

    def test_a_sibling_repositorys_error_survives_a_held_lease(self):
        recent = latest(finished_at=(NOW - timedelta(hours=2)).isoformat())
        doc = stream(liveness="running", last=recent, started=started(age_s=300))
        doc["repos"]["acme/other"] = {"latest": None, "runs": [], "error": "latest.json: bad"}
        doc.update(error="acme/other: latest.json: bad")
        rows = view.stream_rows({"cost-audit": doc}, {})
        self.assertTrue(all(source["error"] for _, _, source in rows))

    def test_died_needs_no_roster_and_no_schedule(self):
        doc = stream(liveness="died", started=started(age_s=9000))
        self.assertEqual(view.flags_for(doc, {}, NOW, True), ["DIED"])
        # It ran: the STATUS cell must not say otherwise beside the flag.
        self.assertEqual(view.status_cell(doc, {}), ("died before finish", "red"))

    def test_an_in_flight_run_never_trips_died(self):
        doc = stream(liveness="running", started=started(age_s=300))
        self.assertEqual(self.flags(doc), [])

    def test_never_fires_when_the_store_was_readable(self):
        self.assertEqual(self.flags(stream(liveness="never")), ["NEVER"])

    def test_never_does_not_fire_when_the_store_was_not(self):
        # The store is the thing that failed; claiming the stream never ran
        # would be the ConfigMap's silent lie in a new place.
        self.assertEqual(self.flags(stream(liveness="never"), root_exists=False), ["NO STORE"])

    def test_a_stream_absent_from_the_projection_reads_as_never(self):
        self.assertEqual(self.flags({}), ["NEVER"])

    def test_a_missed_fire_is_stale(self):
        old = latest(finished_at=(NOW - timedelta(days=3)).isoformat())
        self.assertEqual(self.flags(stream(last=old)), ["STALE"])

    def test_stale_reads_the_schedule_in_the_pod_zone(self):
        # Finished after yesterday's 06:20 in Los Angeles (13:25 UTC). NOW is
        # 05:00 there, before today's fire, but past 06:20 UTC plus slack.
        ran = latest(finished_at=datetime(2026, 8, 25, 13, 25, tzinfo=timezone.utc).isoformat())
        (_, _, source), = view.stream_rows({"cost-audit": stream(last=ran)}, {})
        self.assertEqual(view.flags_for(source, self.JOB, NOW, True), ["STALE"])
        self.assertEqual(
            view.flags_for(source, self.JOB, NOW, True, schedule_tz=LOS_ANGELES), []
        )

    def test_the_timezone_flag_defaults_to_utc_and_refuses_an_unknown_zone(self):
        parser = view.build_parser()
        self.assertEqual(parser.parse_args([]).timezone, timezone.utc)
        self.assertEqual(
            parser.parse_args(["--timezone", "America/Los_Angeles"]).timezone, LOS_ANGELES
        )
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            parser.parse_args(["--timezone", "Mars/Olympus_Mons"])
        self.assertIn("unknown time zone", err.getvalue())

    def test_the_default_zone_needs_no_tz_database(self):
        # Windows without `tzdata`: every ZoneInfo lookup fails, and a run with
        # no `--timezone` must still start. A named zone still fails cleanly.
        missing = ZoneInfoNotFoundError("no time zone found")
        with mock.patch.object(view, "ZoneInfo", side_effect=missing):
            parser = view.build_parser()
            self.assertEqual(parser.parse_args([]).timezone, timezone.utc)
            self.assertEqual(parser.parse_args(["--timezone", "UTC"]).timezone, timezone.utc)
            with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                parser.parse_args(["--timezone", "America/Los_Angeles"])
        self.assertIn("unknown time zone", err.getvalue())

    def test_the_timezone_flag_reaches_stale_through_main(self):
        # The parser and flags_for are pinned above; this is the join between
        # them: `--timezone` on the command line decides STALE in the output.
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is None else NOW.astimezone(tz)

        ran = latest(finished_at=datetime(2026, 8, 25, 13, 25, tzinfo=timezone.utc).isoformat())
        fake = FakeKubectl(
            exec_stdout=json.dumps(projection({"cost-audit": stream(last=ran)}))
        )
        with TemporaryDirectory() as tmp:
            roster = Path(tmp) / "jobs.json"
            job = {
                "id": "cost-audit", "enabled": True, "skills": ["fleet-audit"],
                "schedule": {"expr": self.JOB["expr"]},
            }
            roster.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
            with mock.patch.object(view, "datetime", Frozen):
                _, utc_out, _ = run_main(["--roster", str(roster), "--color", "never"], fake)
                _, la_out, _ = run_main(
                    ["--roster", str(roster), "--color", "never",
                     "--timezone", "America/Los_Angeles"],
                    fake,
                )
        self.assertIn("STALE", utc_out)
        self.assertNotIn("STALE", la_out)

    def test_a_stream_still_running_is_late_not_stale(self):
        # The lease says it is not silent; the STATUS cell carries its age.
        old = latest(finished_at=(NOW - timedelta(days=3)).isoformat())
        doc = stream(liveness="running", last=old, started=started(age_s=3900))
        self.assertEqual(self.flags(doc), [])

    def test_stale_waits_for_the_leases(self):
        # With the scratch directory unlisted a run in flight reads as idle,
        # so silence cannot be told from a run that is going right now.
        old = latest(finished_at=(NOW - timedelta(days=3)).isoformat())
        (_, _, source), = view.stream_rows({"cost-audit": stream(last=old)}, {})
        self.assertEqual(view.flags_for(source, self.JOB, NOW, True, leases_read=False), [])

    def test_a_disabled_stream_abstains_from_stale_and_never(self):
        old = latest(finished_at=(NOW - timedelta(days=30)).isoformat())
        self.assertEqual(self.flags(stream(last=old), {"enabled": False}), [])
        self.assertEqual(self.flags(stream(liveness="never"), {"enabled": False}), [])


class TestProducerRoundTrip(unittest.TestCase):
    """The view over a projection `report_status.project` made from an
    envelope `audit_report.write_report` stored, not a hand-built one. The
    view streams the projection script into a pod rather than importing it,
    so a key renamed on one side reaches no import error: only this."""

    ROSTER = {"compliance-audit": {"enabled": True, "expr": "20 6 * * *"}}
    FINISHED = datetime(2026, 8, 26, 6, 31, tzinfo=timezone.utc)

    def setUp(self):
        self.root = mkdtemp(prefix="view-store-")
        self.scratch = mkdtemp(prefix="view-scratch-")
        for path in (self.root, self.scratch):
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"FLEET_AUDIT_REPORTS_DIR": self.root})
        env.start()
        self.addCleanup(env.stop)
        findings = [
            {"id": "a1", "severity": "critical", "title": "a"},
            {"id": "b2", "severity": "major", "title": "b"},
            {"id": "c3", "severity": "minor", "title": "c"},
        ]
        envelope = audit_report.report_envelope(
            "compliance-audit",
            {
                "status": "UPDATED",
                "partial": False,
                "coverage_gaps": [],
                "issue_url": "https://github.com/acme/fleet/issues/12",
                "prs_opened": ["https://github.com/acme/fleet/pull/9"],
            },
            {"findings": findings, "scope": {"clusters": [{"name": "prod"}], "skipped": []}},
            self.FINISHED,
            repo="acme/fleet",
            issue_number=12,
            ledger_body="body",
            new_ids=["a1", "b2"],
            resolved_ids=["z9"],
            rendered_ids=["a1", "b2", "c3"],
        )
        audit_report.write_report("compliance-audit", envelope, self.FINISHED)

    def project(self):
        return report_status.project(self.root, now=NOW, scratch=self.scratch)

    def rows(self, projection):
        return [
            view.row_for(label, source, self.ROSTER[audit_id], NOW, True, True)
            for label, audit_id, source in view.stream_rows(
                projection["streams"], self.ROSTER
            )
        ]

    def cells(self, row):
        titles = [column.title for column in view.COLUMNS]
        return {title: cell[0] for title, cell in zip(titles, row)}

    def test_the_counts_and_the_clean_flags_come_through(self):
        [(row, flags, latest)] = self.rows(self.project())
        self.assertEqual(flags, [])
        self.assertEqual(latest["findings"], 3)
        self.assertEqual(latest["critical"], 1)
        cells = self.cells(row)
        self.assertEqual(cells["STATUS"], "UPDATED")
        self.assertEqual(cells["FINDINGS"], "3 (1 c)")
        self.assertEqual(cells["Δ"], "+2 / −1")
        self.assertEqual(cells["PRS"], "1")
        self.assertEqual(cells["ISSUE"], "#12")
        out = view.render(
            self.project(), self.ROSTER, NOW, str(view.REPO_ROOT / "scripts" / "jobs.json"),
            "ns/agent-0 [platform-agent]",
        )
        self.assertIn("3 (1 c)", out)
        self.assertIn("1 critical", out)

    def test_a_ring_without_latest_is_unrecorded(self):
        (Path(self.root) / "compliance-audit" / "acme" / "fleet" / "latest.json").unlink()
        [(row, flags, latest)] = self.rows(self.project())
        self.assertEqual(flags, ["UNRECORDED"])
        self.assertEqual(self.cells(row)["FINDINGS"], "3 (1 c)")

    def test_a_running_lease_comes_through(self):
        (Path(self.scratch) / "inflight_compliance-audit.json").write_text(
            json.dumps({"audit": "compliance-audit", "started_at": NOW.timestamp() - 90}),
            encoding="utf-8",
        )
        [(row, flags, _)] = self.rows(self.project())
        self.assertEqual(self.cells(row)["STATUS"], "running… 1m30s")
        self.assertEqual(flags, [])


class TestRender(unittest.TestCase):
    ROSTER = {"compliance-audit": {"enabled": True, "expr": "20 6 * * *"}}

    # Absolute, because `short_path` resolves a relative one against the
    # working directory: passing a bare "jobs.json" made the rendered header
    # depend on where the suite was started from, so this file passed on its
    # own and failed under `make test-python`.
    ROSTER_PATH = str(view.REPO_ROOT / "scripts" / "jobs.json")

    def render(self, streams, roster=None, root_exists=True, **kwargs):
        return view.render(
            projection(streams, root_exists=root_exists),
            self.ROSTER if roster is None else roster,
            NOW,
            self.ROSTER_PATH,
            "ns/agent-0 [platform-agent]",
            **kwargs,
        )

    def test_a_full_row_renders_its_fields(self):
        out = self.render({"compliance-audit": stream(last=latest())})
        self.assertIn("compliance-audit", out)
        self.assertIn("UPDATED", out)
        self.assertIn("57 (2 c)", out)
        self.assertIn("+3 / −1", out)
        self.assertIn("#12", out)

    def test_a_withheld_delta_renders_as_unknown_not_zero(self):
        # `finish` stores empty id lists over a lost memory; the envelope's
        # `delta_known: false` is what tells that from a run that changed nothing.
        out = self.render(
            {"compliance-audit": stream(last=latest(new=0, resolved=0, delta_known=False))}
        )
        self.assertNotIn("+0 / −0", out)

    def test_an_envelope_without_delta_known_renders_its_counts(self):
        out = self.render({"compliance-audit": stream(last=latest(delta_known=None))})
        self.assertIn("+3 / −1", out)

    def test_a_stream_on_two_repositories_is_two_labelled_rows(self):
        doc = stream(last=latest())
        doc["repos"]["acme/other"] = {
            "latest": latest(status="CLEAN", issue_url="https://github.com/acme/other/issues/7"),
            "runs": [],
            "error": None,
        }
        out = self.render({"compliance-audit": doc})
        self.assertIn("compliance-audit acme/fleet", out)
        self.assertIn("compliance-audit acme/other", out)
        self.assertIn("#7", out)
        self.assertIn("#12", out)

    def test_an_abandoned_repository_does_not_make_the_stream_stale(self):
        # The store never prunes a repository's directory; one the stream
        # stopped publishing to keeps a weeks-old row beside the current one.
        doc = stream(last=latest())
        doc["repos"]["acme/gone"] = {
            "latest": latest(repo="acme/gone", finished_at="2026-07-01T06:31:00+00:00"),
            "runs": [],
            "error": None,
            "latest_missing": False,
        }
        out = self.render({"compliance-audit": doc})
        self.assertIn("compliance-audit acme/gone", out)
        self.assertNotIn("STALE", out)
        # Every repository silent is the stream silent, on each of its rows.
        doc["repos"]["acme/fleet"]["latest"] = latest(finished_at="2026-07-02T06:31:00+00:00")
        out = self.render({"compliance-audit": doc})
        self.assertEqual(out.count("STALE"), 2)

    def test_a_held_run_is_a_known_status(self):
        out = self.render({"compliance-audit": stream(last=latest(status="HELD"))})
        self.assertIn("HELD", out)
        self.assertNotIn("HELD ?", out)

    def test_link_targets_are_scrubbed(self):
        hostile = "https://github.com/acme/fleet/issues/12\x1b]52;c;cHduZWQ=\x07"
        out = self.render(
            {"compliance-audit": stream(last=latest(issue_url=hostile, prs_opened=[hostile]))},
            palette=view.Palette(True),
        )
        self.assertNotIn("\x1b]52", out)
        self.assertNotIn("\x07", out)

    def test_a_non_count_critical_is_a_missing_one(self):
        # Rendered bare into FINDINGS, a string here was an escape in the terminal.
        out = self.render(
            {"compliance-audit": stream(last=latest(critical="\x1b]0;x\x07"))},
            palette=view.Palette(True),
        )
        self.assertNotIn("\x1b]0", out)
        self.assertNotIn("\x07", out)
        self.assertIn("57", out)
        self.assertNotIn(" c)", out)

    def test_the_schedule_cell_is_scrubbed(self):
        roster = {"compliance-audit": {"enabled": True, "expr": "20 6 * * *\x1b]0;x\x07"}}
        out = self.render({"compliance-audit": stream(last=latest())}, roster=roster)
        self.assertNotIn("\x1b]0", out)
        self.assertNotIn("\x07", out)

    def test_a_non_finite_lease_age_renders_as_unknown(self):
        for age in (float("nan"), float("inf")):
            doc = stream(liveness="running", started={"age_s": age})
            self.assertEqual(view.status_cell(doc, {}), ("running… ?", "yellow"))

    def test_the_prs_column_counts_the_url_list(self):
        urls = ["https://x/pull/1", "https://x/pull/2"]
        out = self.render({"compliance-audit": stream(last=latest(prs_opened=urls))})
        row = next(line for line in out.splitlines() if "compliance-audit" in line and "│" in line)
        cells = [cell.strip() for cell in terminal_table.plain(row).strip("│").split("│")]
        titles = [column.title for column in view.COLUMNS]
        self.assertEqual(cells[titles.index("PRS")], "2")

    def test_the_header_names_the_store_and_the_source(self):
        out = self.render({})
        self.assertIn("/opt/data/fleet-audit/reports", out)
        self.assertIn("ns/agent-0 [platform-agent]", out)
        # Shortened to repo-relative: the real default is an absolute path
        # eighty characters long on a worktree checkout.
        self.assertRegex(out, r"roster\s+scripts/jobs\.json")

    def test_a_rostered_stream_with_no_files_reads_never_ran(self):
        out = self.render({})
        self.assertIn("never ran", out)
        self.assertIn("NEVER", out)

    def test_partial_runs_warn_and_print_their_gaps(self):
        gaps = ["prod-eu-1: API server unreachable"]
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=gaps))},
            show_gaps=True,
        )
        self.assertIn("⚠", out)
        self.assertIn("COVERAGE GAPS", out)
        self.assertIn("prod-eu-1", out)

    def test_the_default_view_counts_the_gaps_it_is_not_printing(self):
        """Behind a flag, but never off-screen.

        A run that read less than the fleet has to say so in the view an
        operator gets with no arguments; what `--gaps` buys is the collector's
        own wording, not the existence of the gap.
        """
        gaps = ["prod-eu-1: API server unreachable", "prod-us-2: quota exhausted"]
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=gaps))}
        )
        self.assertIn("2 coverage gaps in 1 stream; --gaps for the text", out)
        self.assertNotIn("API server unreachable", out)
        self.assertNotIn("COVERAGE GAPS", out)

    def test_the_gap_count_is_singular_for_one(self):
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=["a: b"]))}
        )
        self.assertIn("1 coverage gap in 1 stream;", out)

    def test_a_filtered_out_stream_does_not_contribute_its_gaps(self):
        # The count sits under the table and has to describe the same rows.
        doc = {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=["a: b"]))}
        self.assertNotIn("coverage gap", self.render(doc, patterns=("cost",)))

    def test_the_scope_is_split_out_of_a_gap_that_has_one(self):
        out = self.render(
            {
                "compliance-audit": stream(
                    last=latest(partial=True, coverage_gaps=["prod-eu-1: quota exhausted"])
                )
            },
            show_gaps=True,
        )
        printed = next(line for line in out.splitlines() if "quota exhausted" in line)
        self.assertRegex(terminal_table.plain(printed), r"prod-eu-1\s+│\s+quota exhausted")

    def test_a_gap_that_is_a_sentence_is_not_split_at_its_colon(self):
        gap = "partially audited — 3 checks did not run: release-channel, node-image"
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=[gap]))},
            show_gaps=True,
        )
        self.assertIn("partially audited — 3 checks did not run: release-channel", out)

    def test_an_unknown_status_renders_as_a_warning_not_success(self):
        out = self.render({"compliance-audit": stream(last=latest(status="SOMETHING_NEW"))})
        self.assertIn("SOMETHING_NEW ?", out)

    def test_a_held_stream_reads_as_running_with_the_leases_age(self):
        doc = stream(liveness="running", started=started(300), last=latest())
        self.assertIn("running… 5m00s", self.render({"compliance-audit": doc}))

    def test_a_first_run_in_flight_reads_as_running_not_never(self):
        doc = stream(liveness="running", started=started(30))
        out = self.render({"compliance-audit": doc})
        self.assertIn("running… 30s", out)
        self.assertNotIn("NEVER", out)

    def test_a_stream_error_reaches_the_status_cell(self):
        doc = stream(liveness="error", error="latest.json: not a JSON object")
        out = self.render({"compliance-audit": doc})
        # In the row itself: the footer prints the same text, so the whole
        # output carries it even when the cell does not.
        row = next(line for line in out.splitlines() if "compliance-audit" in line and "│" in line)
        self.assertIn("latest.json: not a JSON object", row)
        self.assertIn("NO STORE", row)

    def test_a_stream_error_beside_a_completed_run_is_printed(self):
        # STATUS shows the run's status, so the error text has to reach the
        # footer or the operator sees a bare NO STORE with no reason.
        doc = stream(last=latest(), error="Acme/Fleet: not lower-case, so no reader opens it")
        out = self.render({"compliance-audit": doc})
        self.assertIn("UPDATED", out)
        self.assertIn("Acme/Fleet: not lower-case", out)

    def test_an_unreadable_lease_directory_is_named_not_the_stores(self):
        lease = "/opt/data/scratch/: Permission denied"
        doc = stream(liveness="error", last=latest(), error=lease)
        out = view.render(
            projection({"compliance-audit": doc}, lease_error=lease),
            self.ROSTER, NOW, self.ROSTER_PATH, "ns/agent-0 [platform-agent]",
        )
        self.assertIn("in-flight leases unreadable", out)
        self.assertNotIn("unreadable stream files", out)

    def test_a_roster_stream_is_not_never_while_the_leases_are_unread(self):
        # Its first run may be in flight with no store directory yet; the lease
        # that would say so could not be listed.
        out = view.render(
            projection({}, lease_error="/opt/data/scratch/: Permission denied"),
            self.ROSTER, NOW, self.ROSTER_PATH, "ns/agent-0 [platform-agent]",
        )
        # The lead's caveat names the flag; the table must not raise it.
        table = out.split("STREAMS", 1)[1].split("\n!", 1)[0]
        self.assertNotIn("NEVER", table)

    def test_the_lead_is_not_all_clear_while_the_leases_are_unread(self):
        # No stream directory yet, so no row carries the lease error to raise
        # NO STORE; the lead has to say the silent-stream flags were not run.
        out = view.render(
            projection({}, lease_error="/opt/data/scratch/: Permission denied"),
            self.ROSTER, NOW, self.ROSTER_PATH, "ns/agent-0 [platform-agent]",
        )
        lead = out.splitlines()[0]
        self.assertNotIn("all clear", lead)
        self.assertIn("leases unreadable — NEVER and STALE not checked", lead)

    def test_a_hidden_repository_row_is_counted_as_a_row(self):
        doc = stream(last=latest())
        doc["repos"]["acme/other"] = {"latest": latest(), "runs": [], "error": None}
        out = self.render({"compliance-audit": doc}, patterns=("acme/other",))
        self.assertIn("1 of 2 rows shown", out)

    def test_a_missing_store_says_so_below_the_table(self):
        out = self.render({}, root_exists=False)
        self.assertIn("store directory absent on the pod", out)

    def test_an_unlistable_store_is_unreadable_not_absent(self):
        root_error = "Permission denied"
        out = view.render(
            projection({}, root_exists=False, root_error=root_error),
            self.ROSTER, NOW, self.ROSTER_PATH, "ns/agent-0 [platform-agent]",
        )
        line = next(l for l in out.splitlines() if "store directory unreadable" in l)
        self.assertIn("/opt/data/fleet-audit/reports: Permission denied", line)
        self.assertEqual(line.count("/opt/data/fleet-audit/reports"), 1)
        self.assertNotIn("absent", out)

    def test_a_long_coverage_gap_is_clipped(self):
        """Even opened deliberately, the section has a ceiling.

        The live install writes four-sentence gaps explaining a refused `gcloud`
        flag; six of those unclipped scroll everything above them off the
        terminal. The full text stays in the envelope for `fleet-audit-reports`.
        """
        gap = "prod-eu-1: " + "the api server refused the read. " * 20
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=[gap]))},
            show_gaps=True,
        )
        printed = next(line for line in out.splitlines() if "prod-eu-1" in line)
        cell = terminal_table.plain(printed).split("│")[3].strip()
        self.assertLessEqual(len(cell), view.GAP_WIDTH)
        self.assertTrue(cell.endswith("…"))

    def test_a_multi_line_coverage_gap_stays_on_one_line(self):
        # A collector that writes a newline into a gap would otherwise split the
        # cell across two paragraphs and read as two gaps.
        gap = "prod-eu-1:\nthe api server\nrefused the read"
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=[gap]))},
            show_gaps=True,
        )
        self.assertIn("the api server refused the read", out)

    def test_a_short_coverage_gap_is_printed_whole(self):
        gap = "prod-eu-1: API server unreachable"
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=[gap]))},
            show_gaps=True,
        )
        self.assertIn("API server unreachable", out)
        self.assertNotIn("…", out)

    def test_coverage_gaps_are_scrubbed(self):
        gaps = ["bad\x1b]8;;x\x07gap"]
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=gaps))},
            show_gaps=True,
        )
        self.assertNotIn("\x1b", out)

    def test_a_stream_label_is_scrubbed_in_the_gaps_table(self):
        # The gaps table prints the label outside `row_for`, so the scrub at
        # the row boundary is the only one it passes.
        out = self.render(
            {"bad\x1b]8;;x\x07audit": stream(last=latest(partial=True, coverage_gaps=["x: y"]))},
            show_gaps=True,
        )
        self.assertNotIn("\x1b", out)

    def test_last_run_uses_the_system_local_zone_by_default(self):
        # No tz argument: local_time() must consult the machine's zone, not a
        # hardcoded one — assert it against the same conversion, not a fixed
        # clock time, so the test does not encode any particular zone either.
        at = datetime.fromisoformat(latest()["finished_at"])
        out = self.render({"compliance-audit": stream(last=latest())})
        self.assertIn(view.local_time(at), out)

    def test_local_time_honors_an_explicit_zone(self):
        # 06:31 UTC on 2026-08-26 is 2:31 am US/Eastern (EDT) — used here only
        # to prove the conversion works, not as the tool's default.
        at = datetime.fromisoformat(latest()["finished_at"])
        self.assertEqual(
            view.local_time(at, timezone(timedelta(hours=-4))), "Aug 26 2:31 am"
        )


class TestDashboard(unittest.TestCase):
    """The presentation half: borders, colour, links, filters, sort."""

    ROSTER = {
        "compliance-audit": {"enabled": True, "expr": "20 6 * * *"},
        "cost-audit": {"enabled": True, "expr": "20 6 * * *"},
    }

    def render(self, streams, roster=None, **kwargs):
        return view.render(
            projection(streams),
            self.ROSTER if roster is None else roster,
            NOW,
            str(view.REPO_ROOT / "scripts" / "jobs.json"),
            "src",
            **kwargs,
        )

    def body_rows(self, out):
        return [
            line for line in out.splitlines()
            if line.startswith("│") and "STREAM" not in line
        ]

    def two(self):
        return {
            "compliance-audit": stream(last=latest(findings=57, critical=2)),
            "cost-audit": stream(
                last=latest(audit_id="cost-audit", findings=3, critical=0, issue_number=8)
            ),
        }

    def test_a_stream_on_two_repositories_counts_once_in_the_header(self):
        streams = self.two()
        streams["cost-audit"]["repos"]["acme/other"] = {
            "latest": latest(audit_id="cost-audit", repo="acme/other"), "runs": [], "error": None,
        }
        out = self.render(streams)
        self.assertEqual(len(self.body_rows(out)), 3)
        self.assertIn("2 streams", out)
        self.assertNotIn("3 streams", out)
        self.assertIn("across 2 run streams", out)

    def test_a_stream_on_two_repositories_counts_once_in_the_scope_line(self):
        streams = self.two()
        streams["cost-audit"]["repos"]["acme/other"] = {
            "latest": latest(audit_id="cost-audit", repo="acme/other", clusters=9, skipped=2),
            "runs": [],
            "error": None,
        }
        scope = next(
            line for line in terminal_table.plain(self.render(streams)).splitlines()
            if line.strip().startswith("scope")
        )
        self.assertIn("9 units widest", scope)
        self.assertIn("across 2 run streams", scope)
        self.assertIn("2 skipped", scope)

    def test_a_stream_on_two_repositories_counts_once_in_the_gap_count(self):
        streams = {"cost-audit": stream(last=latest(audit_id="cost-audit", coverage_gaps=["a: b"]))}
        streams["cost-audit"]["repos"]["acme/other"] = {
            "latest": latest(audit_id="cost-audit", repo="acme/other", coverage_gaps=["a: b"]),
            "runs": [],
            "error": None,
        }
        out = self.render(streams)
        self.assertIn("2 coverage gaps in 1 stream;", out)
        self.assertNotIn("in 2 streams", out)

    def test_the_table_is_drawn_with_box_borders(self):
        out = self.render(self.two())
        self.assertIn("┌", out)
        self.assertIn("│", out)
        self.assertIn("└", out)

    def test_ascii_swaps_the_box_characters_out(self):
        out = self.render(self.two(), box=view.BOX_ASCII)
        self.assertNotIn("┌", out)
        self.assertIn("+-", out)

    def border_widths(self, out):
        """Every border line's width in *columns*, which is the only measure
        that can catch this.

        Measuring with `len(plain(...))` — what this did until an emoji in a
        cell was tried — is the renderer's own arithmetic, so the assertion
        agreed with the bug: a cell one column wide per character came back
        the same length as the border that failed to contain it.
        """
        return {
            terminal_table.display_width(line)
            for line in out.splitlines()
            if terminal_table.plain(line).startswith(("┌", "│", "├", "└"))
        }

    def test_every_border_line_is_the_same_width(self):
        # The one failure a coloured table produces silently: a cell measured
        # with its escape sequences included pads short and the column below
        # it steps sideways.
        out = self.render(self.two(), palette=view.Palette(True))
        self.assertEqual(len(self.border_widths(out)), 1, self.border_widths(out))

    def test_a_cell_whose_characters_are_not_one_column_wide_still_aligns(self):
        """A character is not a column, and both directions were reachable.

        Cell text arrives from a model-written finding title and from GitHub
        pull-request titles, where an emoji is ordinary. Each one drew two
        columns and counted as one character, so the row ran past its own
        border; a combining accent did the reverse.
        """
        for label, name in (
            ("emoji", "\U0001f680 compliance-audit"),
            ("cjk", "コンプライアンス監査"),
            ("combining", "compliance-áudit"),
            ("zero-width joiner", "compliance\u200daudit"),
        ):
            with self.subTest(cell=label):
                streams = {name: stream(last=latest(audit_id=name))}
                out = self.render(streams, roster={name: {"enabled": True, "expr": "20 6 * * *"}})
                widths = self.border_widths(out)
                self.assertEqual(len(widths), 1, "%s: %s" % (label, sorted(widths)))

    def test_a_wide_cell_that_wraps_still_aligns(self):
        # `textwrap` counts characters too, so a wrapped line of wide text fits
        # by its measure and overflows by the terminal's. GAP is a wrapping
        # column; STREAM is not, so a long stream name never reaches the wrap.
        gap = "dr-west: " + "コンプライアンス" * 12
        streams = {"compliance-audit": stream(last=latest(coverage_gaps=[gap]))}
        out = self.render(streams, show_gaps=True, width=100)
        gap_lines = out[out.index("COVERAGE GAPS"):]
        self.assertGreater(sum("コ" in line for line in gap_lines.splitlines()), 1, "never wrapped")
        widths = self.border_widths(gap_lines)
        self.assertEqual(len(widths), 1, sorted(widths))

    def test_colour_is_off_unless_asked_for(self):
        self.assertNotIn("\x1b[", self.render(self.two()))

    def test_the_issue_cell_is_a_hyperlink_when_colour_is_on(self):
        out = self.render(self.two(), palette=view.Palette(True))
        self.assertIn("\x1b]8;;https://github.com/acme/fleet/issues/12\x1b\\", out)

    def test_a_lone_pull_request_is_linked_and_the_list_is_printed(self):
        out = self.render(self.two())
        self.assertIn("PULL REQUESTS OPENED", out)
        self.assertIn("acme/fleet#9", out)
        # The PRS cell's count is itself the link when there is only one.
        coloured = self.render(self.two(), palette=view.Palette(True))
        row = next(line for line in coloured.splitlines() if "cost-audit" in line and "│" in line)
        self.assertIn("\x1b]8;;https://github.com/acme/fleet/pull/9\x1b\\1\x1b]8;;\x1b\\", row)

    def test_the_pull_request_list_pads_by_display_width(self):
        """A wide stream id counts one character per glyph and draws two:
        padded by `len`, its link column sits left of the others'."""
        streams = {
            "cost-audit": stream(last=latest(audit_id="cost-audit")),
            "審計審計": stream(last=latest(audit_id="審計審計")),
        }
        out = terminal_table.plain(self.render(streams))
        listed = out.split("PULL REQUESTS OPENED", 1)[1].splitlines()[1:3]
        starts = {
            terminal_table.display_width(line[: line.index("acme/fleet#9")]) for line in listed
        }
        self.assertEqual(len(starts), 1, listed)

    def test_stream_filters_by_substring_and_says_what_it_hid(self):
        out = self.render(self.two(), patterns=("cost",))
        self.assertIn("cost-audit", out)
        self.assertNotIn("compliance-audit", out)
        self.assertIn("1 of 2 rows shown", out)

    def test_flagged_keeps_only_the_rows_worth_looking_at(self):
        streams = self.two()
        streams["cost-audit"] = stream(liveness="never")
        out = self.render(streams, flagged_only=True)
        self.assertIn("cost-audit", out)
        self.assertNotIn("compliance-audit", out)

    def test_sort_findings_puts_the_worst_stream_first(self):
        streams = self.two()
        streams["cost-audit"] = stream(last=latest(findings=900, critical=9))
        out = self.render(streams, sort="findings")
        self.assertIn("cost-audit", self.body_rows(out)[0])

    def test_sort_stream_is_alphabetical(self):
        streams = self.two()
        streams["cost-audit"] = stream(last=latest(findings=900, critical=9))
        out = self.render(streams, sort="stream")
        self.assertIn("compliance-audit", self.body_rows(out)[0])

    def test_the_header_counts_findings_and_streams_needing_attention(self):
        out = self.render(self.two())
        self.assertIn("2 streams", out)
        self.assertIn("all clear", out)
        self.assertIn("60", out)  # 57 + 3 findings across both
        self.assertIn("2 critical", out)

    def test_the_header_reports_the_widest_scope_not_the_sum(self):
        """Most streams audit the same fleet, so adding their
        scopes together would report one 16-cluster fleet as a hundred and
        fifty clusters audited."""
        streams = {
            "compliance-audit": stream(last=latest(clusters=16)),
            "cost-audit": stream(last=latest(audit_id="cost-audit", clusters=43)),
        }
        out = self.render(streams)
        self.assertIn("43 units", out)
        self.assertIn("widest", out)
        self.assertNotIn("59 units", out)

    def test_the_header_says_nothing_about_scope_when_no_stream_has_run(self):
        self.assertNotIn("units", self.render({"compliance-audit": stream(liveness="never")}))

    def test_a_skipped_cluster_is_surfaced_in_the_header(self):
        out = self.render({"compliance-audit": stream(last=latest(clusters=15, skipped=2))})
        self.assertIn("2 skipped", out)

    def test_scope_stays_out_of_the_per_stream_row(self):
        """It is a header fact, not a row fact: a reader scanning for what
        broke does not need every row to restate how many clusters the fleet
        has."""
        out = self.render({"compliance-audit": stream(last=latest(clusters=16, skipped=2))})
        self.assertNotIn("SCOPE", out)
        self.assertNotIn("16/18", out)

    def held_open_clean(self, current=21, issue_number=12):
        """The PR's live shape: a clean run that left the ledger open over the
        previous run's findings without rewriting it."""
        return latest(
            audit_id="cost-audit", status="CLEAN", ledger_held_open=True, issue_number=issue_number,
            findings=0, critical=0, current=current, prs_opened=[], new=0, resolved=0,
        )

    def test_a_held_open_ledger_shows_what_it_carries_not_this_run_s_zero(self):
        streams = self.two()
        streams["cost-audit"] = stream(last=self.held_open_clean())
        out = self.render(streams)
        row = next(r for r in self.body_rows(out) if "cost-audit" in r)
        self.assertIn("21 held", row)
        # 57 on the compliance ledger plus the 21 the cost ledger still lists.
        self.assertIn("78", out)
        self.assertIn("1 ledger held open", out)
        self.assertIn("1 need attention", out)
        self.assertNotIn("all clear", out)

    def test_a_hold_over_a_lost_memory_is_unknown_not_zero(self):
        """`finish` stores no issue for a hold whose memory was lost."""
        streams = {"cost-audit": stream(last=self.held_open_clean(current=0, issue_number=None))}
        row = next(r for r in self.body_rows(self.render(streams)) if "cost-audit" in r)
        self.assertIn("held ?", row)

    def test_an_unknown_ledger_count_makes_the_total_a_floor(self):
        streams = self.two()
        streams["cost-audit"] = stream(last=self.held_open_clean(current=0, issue_number=None))
        out = self.render(streams)
        findings = next(line for line in out.splitlines() if "across 2 run streams" in line)
        self.assertIn("57+", findings)
        self.assertIn("1 ledger count unknown", findings)

    def test_a_trusted_hold_of_an_empty_ledger_is_zero_not_unknown(self):
        """A coverage issue lists no finding; held open over a trusted memory
        of it, the store knows that zero."""
        streams = {"cost-audit": stream(last=self.held_open_clean(current=0))}
        row = next(r for r in self.body_rows(self.render(streams)) if "cost-audit" in r)
        self.assertIn("0 held", row)
        self.assertNotIn("held ?", row)

    def test_a_held_run_with_no_gap_needs_attention(self):
        """HELD is `partial: false` when there is no coverage gap, and sets no
        flag, yet the run refused to close the ledger."""
        streams = self.two()
        streams["cost-audit"] = stream(
            last=latest(audit_id="cost-audit", status="HELD", partial=False, prs_opened=[])
        )
        out = self.render(streams)
        self.assertIn("1 need attention", out)
        self.assertNotIn("all clear", out)
        self.assertIn("1 of 2 streams clean", out)
        flagged = self.render(streams, flagged_only=True)
        self.assertIn("cost-audit", flagged)
        self.assertNotIn("compliance-audit", "\n".join(self.body_rows(flagged)))

    def test_a_flagged_stream_is_counted_in_the_lead(self):
        streams = self.two()
        streams["cost-audit"] = stream(liveness="never")
        self.assertIn("1 need attention", self.render(streams))

    def test_the_context_is_named_when_one_was_read(self):
        self.assertIn("gke_p_z_hub", self.render({}, context="gke_p_z_hub"))

    def test_utc_swaps_the_clock_out_of_local_time(self):
        out = self.render(self.two(), utc=True)
        self.assertIn("Aug 26 06:31", out)

    def test_a_narrow_width_drops_columns_and_says_which(self):
        out = self.render(self.two(), width=100)
        self.assertIn("dropped to fit 100 columns", out)


class TestContextDiscovery(unittest.TestCase):
    """The commonest reason this view "does not work": the kubeconfig's
    current context is one of the managed clusters, not the hub."""

    class Probing(FakeKubectl):
        """A kubeconfig where only `hubs` hold an agent pod."""

        hubs = ()

        def __call__(self, cmd, **kwargs):
            if "--context" in cmd and cmd[cmd.index("--context") + 1] in self.hubs:
                self.calls.append(
                    {"cmd": list(cmd), "input": None, "timeout": kwargs.get("timeout")}
                )
                if "exec" in cmd:
                    return CompletedProcess(cmd, 0, self.exec_stdout, "")
                return CompletedProcess(cmd, 0, "agent-0 shell\n", "")
            return super().__call__(cmd, **kwargs)

    def probing(self, hubs, **kwargs):
        fake = self.Probing(pods=(), current="managed", **kwargs)
        fake.hubs = hubs
        return fake

    def test_the_failure_names_the_context_it_read(self):
        fake = FakeKubectl(pods=(), current="gke_p_z_managed")
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertIn("the context read was gke_p_z_managed", err)

    def test_the_one_context_holding_the_pod_is_used_and_named(self):
        fake = self.probing(("hub-b",), contexts=("hub-a", "hub-b"))
        rc, out, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 0)
        # Read there, not merely suggested: the exec has to carry the context.
        self.assertEqual(fake.exec_call["cmd"][1:3], ["--context", "hub-b"])
        # Named in the header, and only there. A note on stderr saying the same
        # thing is one more line between the operator and the table.
        self.assertIn("hub-b", out)
        self.assertEqual(err, "")

    def test_two_contexts_holding_a_pod_is_ambiguous_and_stops(self):
        fake = self.probing(("hub-a", "hub-b"), contexts=("hub-a", "hub-b"))
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertIn("--context hub-a", err)
        self.assertIn("--context hub-b", err)

    def test_an_explicit_context_is_never_second_guessed(self):
        fake = self.probing(("hub-b",), contexts=("hub-a", "hub-b"))
        rc, _, err = run_main(["--roster", NO_ROSTER, "--context", "hub-a"], fake)
        self.assertEqual(rc, 2)
        # hub-b holds the pod and the hint may name it, but nothing reads it:
        # the only exec a fallback would run is the one that must not happen.
        self.assertEqual(fake.cmds("exec"), [])

    def test_json_names_the_context_it_was_redirected_to(self):
        # The file carries no context and `--file` renders it with none, so
        # stderr is the only record that the read went to another cluster.
        fake = self.probing(("hub-b",), contexts=("hub-a", "hub-b"))
        rc, emitted, err = run_main(["--roster", NO_ROSTER, "--json"], fake)
        self.assertEqual(rc, 0)
        json.loads(emitted)
        self.assertIn("on context hub-b", err)

    def test_one_other_context_is_not_reported_as_more_than_one(self):
        # Reachable because an explicit `--context` is never second-guessed:
        # that path raises before probing, so the hint does the probe itself
        # and can come back with exactly one.
        fake = self.probing(("hub-b",), contexts=("hub-a", "hub-b"))
        rc, _, err = run_main(["--roster", NO_ROSTER, "--context", "hub-a"], fake)
        self.assertEqual(rc, 2)
        self.assertIn("--context hub-b", err)
        self.assertIn("on another context:", err)
        self.assertNotIn("other contexts", err)

    def test_no_context_anywhere_says_so_rather_than_offering_nothing(self):
        fake = FakeKubectl(pods=(), contexts=("hub-a",), current="managed")
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertIn("no context in the kubeconfig has one", err)

    def test_a_capped_probe_does_not_claim_no_context_has_one(self):
        many = tuple("ctx-%02d" % i for i in range(view.CONTEXT_PROBE_LIMIT + 2))
        fake = FakeKubectl(pods=(), contexts=many, current="managed")
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertNotIn("no context in the kubeconfig has one", err)
        self.assertIn("none of the first 12 of 14 other contexts", err)

    def test_an_explicit_context_reaches_both_kubectl_calls(self):
        fake = FakeKubectl()
        run_main(["--roster", NO_ROSTER, "--context", "hub"], fake)
        for cmd in (fake.calls[0]["cmd"], fake.exec_call["cmd"]):
            self.assertEqual(cmd[1:3], ["--context", "hub"])

    def test_file_mode_asks_the_kubeconfig_nothing(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(projection()), encoding="utf-8")
            fake = FakeKubectl(pods=())
            rc, _, _ = run_main(["--roster", NO_ROSTER, "--file", str(path)], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [])


class TestProjectionRead(unittest.TestCase):
    def test_the_script_is_streamed_in_on_stdin(self):
        fake = FakeKubectl()
        rc, _, _ = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 0)
        call = fake.exec_call
        self.assertEqual(call["cmd"][-3:], ["--", "python3", "-"])
        self.assertIn("-i", call["cmd"])
        self.assertEqual(call["input"], view.PROJECTION_SCRIPT.read_text(encoding="utf-8"))

    def test_the_sandbox_shell_is_read_ahead_of_the_gateway(self):
        """The store is on the volume `audit_report.py` writes, which is the
        sandbox's when there is one; the gateway's /opt/data is a different
        volume that never holds it."""

        fake = FakeKubectl(
            pods=("agent-gateway", "agent-proxy", "agent-shell-0"),
            containers={
                "agent-gateway": "platform-agent fluent-bit",
                "agent-proxy": "envoy-credential-proxy",
                "agent-shell-0": "shell",
            },
        )
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 0)
        cmd = fake.exec_call["cmd"]
        self.assertIn("agent-shell-0", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "shell")
        # The gateway is the fallback, not a rival worth a note.
        self.assertEqual(err, "")

    def test_without_a_sandbox_the_gateways_agent_container_is_read(self):
        fake = FakeKubectl(pods=("agent-gateway",), containers="platform-agent fluent-bit")
        rc, _, _ = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 0)
        cmd = fake.exec_call["cmd"]
        self.assertEqual(cmd[cmd.index("-c") + 1], "platform-agent")

    def test_a_pod_with_neither_container_is_not_an_agent_pod(self):
        fake = FakeKubectl(pods=("agent-proxy",), containers="envoy-credential-proxy")
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertIn("no agent pod found", err)

    def test_an_explicit_pod_defaults_to_the_sandbox_container(self):
        fake = FakeKubectl()
        run_main(["--roster", NO_ROSTER, "--pod", "agent-shell-0"], fake)
        cmd = fake.exec_call["cmd"]
        self.assertEqual(cmd[cmd.index("-c") + 1], "shell")

    def test_an_explicit_container_overrides_it(self):
        fake = FakeKubectl()
        run_main(["--roster", NO_ROSTER, "--container", "other"], fake)
        cmd = fake.exec_call["cmd"]
        self.assertEqual(cmd[cmd.index("-c") + 1], "other")

    def test_a_container_alone_picks_the_pod_that_has_it(self):
        # The sandbox pod sorts first and has only `shell`; asking for the
        # gateway's container must read the gateway, not fail on the sandbox.
        fake = FakeKubectl(
            pods=("agent-gateway", "agent-shell-0"),
            containers={"agent-gateway": "platform-agent fluent-bit", "agent-shell-0": "shell"},
        )
        rc, _, _ = run_main(["--roster", NO_ROSTER, "--container", "platform-agent"], fake)
        self.assertEqual(rc, 0)
        cmd = fake.exec_call["cmd"]
        self.assertIn("agent-gateway", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "platform-agent")

    def test_discovery_filters_by_label_and_running_phase(self):
        fake = FakeKubectl()
        run_main(["--roster", NO_ROSTER], fake)
        get = fake.calls[0]["cmd"]
        self.assertIn("app.kubernetes.io/name=platform-agent", get)
        self.assertIn("status.phase=Running", get)

    def test_several_running_pods_pick_one_and_say_which(self):
        fake = FakeKubectl(pods=("agent-b", "agent-a"))
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 0)
        self.assertIn("2 Running agent pods", err)
        self.assertIn("agent-a", err)
        self.assertIn("--pod overrides", err)
        self.assertIn("agent-a", fake.exec_call["cmd"])

    def test_an_explicit_pod_skips_discovery(self):
        fake = FakeKubectl()
        run_main(["--roster", NO_ROSTER, "--pod", "agent-9"], fake)
        self.assertEqual(fake.cmds("pods"), [])
        self.assertIn("agent-9", fake.exec_call["cmd"])


class TestWatch(unittest.TestCase):
    """`--watch` is the mode somebody leaves open on a second monitor, so the
    first hiccup — a rolled agent pod, an API server restart, a slept laptop —
    must draw itself into the frame rather than end the session."""

    def watch_once(self, fake):
        """One `--watch` frame: the second `time.sleep` ends the loop the way
        ctrl-c does, so `main` returns after drawing exactly one."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(view.subprocess, "run", fake), \
                mock.patch.object(view.time, "sleep", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = view.main(["--roster", NO_ROSTER, "--watch", "5"])
        return rc, out.getvalue(), err.getvalue()

    def test_a_failed_frame_is_drawn_rather_than_ending_the_watch(self):
        fake = FakeKubectl(exec_rc=1, exec_stderr="Error from server: pod is terminating")
        rc, out, _ = self.watch_once(fake)
        # Reached the sleep at all, which is what says the loop survived.
        self.assertEqual(rc, 2)
        self.assertIn("found but exec failed", out)
        self.assertIn("refreshing every 5s", out)

    def test_a_healthy_frame_still_renders(self):
        doc = projection({"compliance-audit": stream(last=latest())})
        rc, out, _ = self.watch_once(FakeKubectl(exec_stdout=json.dumps(doc)))
        self.assertEqual(rc, 0)
        self.assertIn("compliance-audit", out)
        self.assertIn("refreshing every 5s", out)

    def test_without_watch_the_same_failure_is_stderr_and_exit_2(self):
        fake = FakeKubectl(exec_rc=1, exec_stderr="Error from server: pod is terminating")
        rc, out, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertIn("found but exec failed", err)
        self.assertEqual(out, "")


class TestSubprocessFailures(unittest.TestCase):
    """The two reads that are not probes both used to run untimed and uncaught:
    a hung API server hung the view forever, and a kubectl that is not installed
    reached the operator as a traceback."""

    def test_both_reads_carry_a_timeout(self):
        fake = FakeKubectl()
        run_main(["--roster", NO_ROSTER], fake)
        get, exec_call = fake.calls[0], fake.exec_call
        self.assertEqual(get["timeout"], view.DISCOVER_TIMEOUT)
        self.assertEqual(exec_call["timeout"], view.EXEC_TIMEOUT)

    def test_a_hung_pod_lookup_is_exit_2_and_says_it_timed_out(self):
        def hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        rc, _, err = run_main(["--roster", NO_ROSTER], hang)
        self.assertEqual(rc, 2)
        self.assertIn("timed out", err)
        self.assertIn("agent pod lookup", err)

    def test_a_hung_exec_is_exit_2_and_names_the_projection(self):
        class Hangs(FakeKubectl):
            def __call__(self, cmd, **kwargs):
                if "exec" in cmd:
                    raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
                return super().__call__(cmd, **kwargs)

        rc, _, err = run_main(["--roster", NO_ROSTER], Hangs())
        self.assertEqual(rc, 2)
        self.assertIn("the projection in", err)
        self.assertIn("timed out after %ds" % view.EXEC_TIMEOUT, err)

    def test_no_kubectl_on_the_path_is_a_sentence_not_a_traceback(self):
        def missing(cmd, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "kubectl")

        rc, _, err = run_main(["--roster", NO_ROSTER], missing)
        self.assertEqual(rc, 2)
        self.assertIn("could not run kubectl", err)

    def test_the_pod_s_own_error_text_is_scrubbed_on_the_one_shot_path(self):
        # The exec's stderr and a non-JSON stdout are the pod's words; the
        # one-shot error path prints them to the operator's terminal, so an
        # OSC 52 clipboard write in either must arrive defused.
        osc52 = "\x1b]52;c;cm0gLXJm\x07"
        for fake in (
            FakeKubectl(exec_rc=1, exec_stderr=f"boom {osc52}"),
            FakeKubectl(exec_stdout=f"not json {osc52}"),
        ):
            rc, _, err = run_main(["--roster", NO_ROSTER], fake)
            self.assertEqual(rc, 2)
            self.assertNotIn("\x1b", err)
            self.assertNotIn("\x07", err)

    def test_a_timeout_does_not_send_the_view_probing_other_contexts(self):
        # "I could not ask" is not "nothing is here": the twelve-context sweep
        # is for the second, and running it after a timeout turns one stalled
        # read into twelve.
        calls = []

        def hang(cmd, **kwargs):
            calls.append(list(cmd))
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        run_main(["--roster", NO_ROSTER], hang)
        self.assertEqual([c for c in calls if "config" in c], [])


class TestExitCodes(unittest.TestCase):
    def test_no_pod_is_exit_2_and_names_the_namespace(self):
        rc, _, err = run_main(
            ["--roster", NO_ROSTER, "-n", "nope"], FakeKubectl(pods=())
        )
        self.assertEqual(rc, 2)
        self.assertIn("no agent pod found in namespace nope", err)

    def test_a_failed_exec_is_exit_2_and_carries_kubectls_stderr(self):
        fake = FakeKubectl(exec_rc=1, exec_stderr="Error from server (Forbidden): denied")
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertIn("found but exec failed", err)
        self.assertIn("Forbidden", err)

    def test_output_that_is_not_json_is_exit_2(self):
        fake = FakeKubectl(exec_stdout="python3: command not found")
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertIn("not JSON", err)

    def test_a_missing_store_is_exit_1(self):
        fake = FakeKubectl(exec_stdout=json.dumps(projection(root_exists=False)))
        rc, out, _ = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 1)
        self.assertIn("store directory absent on the pod", out)
        lead = out.splitlines()[0]
        self.assertNotIn("all clear", lead)
        self.assertIn("store absent", lead)
        # And over a root that exists but cannot be listed.
        doc = projection(root_exists=False, root_error="Permission denied")
        rc, out, _ = run_main(["--roster", NO_ROSTER], FakeKubectl(exec_stdout=json.dumps(doc)))
        self.assertEqual(rc, 1)
        self.assertNotIn("all clear", out.splitlines()[0])
        self.assertIn("store unreadable", out.splitlines()[0])

    def test_an_unreadable_stream_is_exit_1(self):
        doc = projection({"compliance-audit": stream(liveness="error", error="boom")})
        rc, _, _ = run_main(["--roster", NO_ROSTER], FakeKubectl(exec_stdout=json.dumps(doc)))
        self.assertEqual(rc, 1)

    def test_an_unreadable_lease_directory_is_exit_1_even_with_no_stream(self):
        doc = projection(lease_error="/opt/data/scratch/: Permission denied")
        rc, out, _ = run_main(["--roster", NO_ROSTER], FakeKubectl(exec_stdout=json.dumps(doc)))
        self.assertEqual(rc, 1)
        self.assertIn("in-flight leases unreadable", out)

    def test_a_kubectl_that_could_not_ask_does_not_probe_other_contexts(self):
        # An expired token on the current context must not send the view to
        # read whichever other context holds an agent pod.
        fake = TestContextDiscovery.Probing(
            pods=(), current="hub", contexts=("hub", "staging"),
            get_rc=1, get_stderr="error: You must be logged in to the server (Unauthorized)",
        )
        fake.hubs = ("staging",)
        rc, _, err = run_main(["--roster", NO_ROSTER], fake)
        self.assertEqual(rc, 2)
        self.assertIn("failed (kubectl exit 1)", err)
        self.assertIn("Unauthorized", err)
        self.assertEqual(fake.cmds("staging"), [])

    def test_a_readable_store_is_exit_0(self):
        doc = projection({"compliance-audit": stream(last=latest())})
        rc, _, _ = run_main(["--roster", NO_ROSTER], FakeKubectl(exec_stdout=json.dumps(doc)))
        self.assertEqual(rc, 0)


class TestOfflineRoundTrip(unittest.TestCase):
    DOC = None

    def setUp(self):
        self.doc = projection({"compliance-audit": stream(last=latest())})

    def test_json_output_is_what_file_consumes(self):
        fake = FakeKubectl(exec_stdout=json.dumps(self.doc))
        rc, emitted, _ = run_main(["--roster", NO_ROSTER, "--json"], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(emitted), self.doc)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "projection.json"
            path.write_text(emitted, encoding="utf-8")
            rc_file, rendered, _ = run_main(
                ["--roster", NO_ROSTER, "--file", str(path)], FakeKubectl(pods=())
            )
        self.assertEqual(rc_file, 0)
        self.assertIn("compliance-audit", rendered)
        self.assertIn("UPDATED", rendered)
        self.assertIn(f"file {path}", rendered)

    def test_file_mode_reaches_no_cluster(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "projection.json"
            path.write_text(json.dumps(self.doc), encoding="utf-8")
            fake = FakeKubectl(pods=())
            rc, out, _ = run_main(["--roster", NO_ROSTER, "--file", str(path)], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("compliance-audit", out)

    def test_stdin_is_a_file_source(self):
        fake = FakeKubectl(pods=())
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(view.subprocess, "run", fake), \
                mock.patch.object(view.sys, "stdin", io.StringIO(json.dumps(self.doc))):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = view.main(["--roster", NO_ROSTER, "--file", "-"])
        self.assertEqual(rc, 0)
        self.assertIn("compliance-audit", out.getvalue())

    def test_an_unreadable_file_is_exit_2(self):
        rc, _, err = run_main(
            ["--roster", NO_ROSTER, "--file", "/nonexistent/projection.json"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("could not read", err)


class TestRosterLoading(unittest.TestCase):
    def test_the_checked_in_roster_yields_every_fleet_audit_stream(self):
        roster, error = view.load_roster(view.DEFAULT_ROSTER)
        self.assertEqual(error, "")
        self.assertGreaterEqual(len(roster), 9)
        self.assertIn("compliance-audit", roster)
        for job in roster.values():
            # A schedule the view can read, not just a key: an empty or
            # unparsable `expr` would leave STALE unable to fire for it.
            self.assertIsNotNone(
                view.next_fire(job["expr"], datetime(2026, 1, 1, tzinfo=timezone.utc)),
                job,
            )

    def test_a_roster_with_no_fleet_audit_job_is_empty_and_not_an_error(self):
        roster, error = view.load_roster(Path(NO_ROSTER))
        self.assertEqual((roster, error), ({}, ""))

    def test_a_missing_roster_reports_why(self):
        roster, error = view.load_roster(Path("/nonexistent"))
        self.assertEqual(roster, {})
        self.assertIn("/nonexistent", error)

    def test_a_bare_list_roster_is_read(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text(json.dumps([{"id": "cost-audit", "skills": ["fleet-audit"], "enabled": True}]), encoding="utf-8")
            roster, error = view.load_roster(path)
        self.assertEqual(error, "")
        self.assertIn("cost-audit", roster)

    def test_a_roster_of_another_shape_reports_why(self):
        for text in ("42", '"jobs"', '{"jobs": {"a": 1}}', "[1, 2]", "{}", '{"job": []}'):
            with self.subTest(text=text), TemporaryDirectory() as tmp:
                path = Path(tmp) / "jobs.json"
                path.write_text(text, encoding="utf-8")
                roster, error = view.load_roster(path)
                self.assertEqual(roster, {})
                self.assertTrue(error)

    def test_a_malformed_roster_reports_why(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text("{not json", encoding="utf-8")
            roster, error = view.load_roster(path)
        self.assertEqual(roster, {})
        self.assertTrue(error)


class TestUnreadableRoster(unittest.TestCase):
    """An unreadable roster disarms NEVER and STALE, so the empty flag list it
    produces must never be rendered as "all clear" — the same lie the retired
    ConfigMap told for thirty hours, told about the other half of the inputs."""

    def run_with_missing_roster(self):
        doc = projection({"compliance-audit": stream(liveness="never")})
        return run_main(
            ["--roster", "/nonexistent/jobs.json"],
            FakeKubectl(exec_stdout=json.dumps(doc)),
        )

    def test_it_is_exit_1_not_exit_0(self):
        rc, _, _ = self.run_with_missing_roster()
        self.assertEqual(rc, 1)

    def test_the_lead_says_the_flags_were_not_checked(self):
        _, out, _ = self.run_with_missing_roster()
        self.assertNotIn("all clear", out)
        self.assertIn("NEVER and STALE not checked", out)

    def test_the_caveat_survives_a_stream_that_needs_attention(self):
        # The case the first cut missed: a partial run elsewhere fills the
        # attention count, and the caveat would then have been dropped — so the
        # header would read "1 need attention" over a fleet where nothing had
        # looked for a silent stream at all.
        doc = projection(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=["x: y"]))}
        )
        rc, out, _ = run_main(
            ["--roster", "/nonexistent/jobs.json"],
            FakeKubectl(exec_stdout=json.dumps(doc)),
        )
        self.assertEqual(rc, 1)
        self.assertIn("need attention", out)
        self.assertIn("NEVER and STALE not checked", out)

    def test_the_roster_field_names_the_reason(self):
        _, out, _ = self.run_with_missing_roster()
        self.assertIn("unreadable", out)
        self.assertIn("/nonexistent/jobs.json", out)

    def test_an_empty_roster_is_still_all_clear(self):
        # The distinction the fix turns on: a file that parses and lists no
        # fleet-audit job is a fleet with nothing scheduled, not a failed read.
        doc = projection({"compliance-audit": stream(last=latest())})
        rc, out, _ = run_main(
            ["--roster", NO_ROSTER], FakeKubectl(exec_stdout=json.dumps(doc))
        )
        self.assertEqual(rc, 0)
        self.assertIn("all clear", out)

    def test_a_store_failure_still_outranks_it(self):
        # Both broken is still exit 1, and the store's own reason still prints.
        fake = FakeKubectl(exec_stdout=json.dumps(projection(root_exists=False)))
        rc, out, _ = run_main(["--roster", "/nonexistent/jobs.json"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("store directory absent on the pod", out)


class TestColourGate(unittest.TestCase):
    class Tty:
        def isatty(self):
            return True

    def test_an_empty_no_color_does_not_disable_colour(self):
        # no-color.org: set and non-empty.
        with mock.patch.dict("os.environ", {"NO_COLOR": "", "TERM": "xterm"}):
            self.assertTrue(view.want_colour("auto", self.Tty()))
        with mock.patch.dict("os.environ", {"NO_COLOR": "1", "TERM": "xterm"}):
            self.assertFalse(view.want_colour("auto", self.Tty()))


class TestFormatting(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(view.duration(214.0), "3m34s")
        self.assertEqual(view.duration(41.5), "41s")
        self.assertEqual(view.duration(None), "?")

    def test_issue_ref(self):
        self.assertEqual(view.issue_ref("https://github.com/a/b/issues/12"), "#12")
        self.assertEqual(view.issue_ref(None), "—")

    def test_zero_width_is_by_category_not_combining_class(self):
        # U+200D, U+200B and U+FE0F have combining class 0 and draw nothing.
        self.assertEqual(terminal_table.display_width("a\u200db\u200bc\ufe0f"), 3)
        self.assertEqual(terminal_table.display_width("e\u0301"), 1)
        # Spacing marks (Mc) with a non-zero combining class still draw: the
        # Javanese virama U+A953 (class 9) one column, the Hangul tone mark
        # U+302E (class 224, East Asian Wide) two.
        self.assertEqual(terminal_table.display_width("a\ua953"), 2)
        self.assertEqual(terminal_table.display_width("a\u302e"), 3)
        # The soft hyphen is Cf too, but terminals draw it as a hyphen.
        self.assertEqual(terminal_table.display_width("co\u00adop"), 5)

    def test_clamped_minimums_do_not_overspend_the_width(self):
        # Two wrap columns; A's proportional share (3) is below its content
        # (5), so it is clamped up to the content, not to min_width, and the
        # excess comes back from B rather than running the table wide.
        columns = [terminal_table.Column("A", wrap=True), terminal_table.Column("B", wrap=True)]
        rows = [[("x" * 5,), ("y" * 50,)]]
        total = 40 + terminal_table._overhead(2)
        widths = terminal_table._resolve_widths(columns, rows, total)
        self.assertEqual(sum(widths), 40)
        self.assertEqual(widths, [5, 35])

    def test_the_rounding_remainder_never_widens_a_column_past_its_content(self):
        # Three equal wrap columns share 59 columns as 19 each, two short. The
        # two go one apiece, and never to a column already at its content.
        columns = [terminal_table.Column(t, wrap=True, min_width=12) for t in "ABC"]
        rows = [[("x" * 20,), ("y" * 20,), ("z" * 20,)]]
        total = 59 + terminal_table._overhead(3)
        widths = terminal_table._resolve_widths(columns, rows, total)
        self.assertEqual(sum(widths), 59)
        self.assertEqual(sorted(widths), [19, 20, 20])

    def test_a_short_wrap_column_is_counted_at_its_content_when_fitting(self):
        # STATUS-like: min_width 11, content 5. The table fits at its natural
        # widths, so no expendable column may be dropped.
        columns = [
            terminal_table.Column("S", wrap=True, min_width=11),
            terminal_table.Column("AGE", expendable=1),
        ]
        rows = [[("CLEAN",), ("3h",)]]
        natural_total = 5 + 3 + terminal_table._overhead(2)
        kept, _, dropped = terminal_table._fit_columns(columns, rows, natural_total)
        self.assertEqual(dropped, [])
        self.assertEqual([c.title for c in kept], ["S", "AGE"])

    def test_nothing_is_dropped_when_dropping_everything_would_not_fit(self):
        # The load-bearing column alone is wider than the terminal: dropping
        # AGE loses it and the table still wraps, so it stays and no note says
        # the table was fitted.
        columns = [
            terminal_table.Column("STREAM"),
            terminal_table.Column("AGE", expendable=1),
        ]
        rows = [[("x" * 60,), ("3h",)]]
        kept, _, dropped = terminal_table._fit_columns(columns, rows, 40)
        self.assertEqual(dropped, [])
        self.assertEqual([c.title for c in kept], ["STREAM", "AGE"])

    def test_a_cell_that_fits_is_not_split_for_its_zero_width_characters(self):
        text = "compliance\u200daudit"
        width = terminal_table.display_width(text)
        lines = terminal_table._cell_lines(text, width)
        self.assertEqual([line for line, _ in lines], [text])

    def test_count_cell(self):
        self.assertEqual(view.count_cell(["a", "b"]), "2")
        self.assertEqual(view.count_cell([]), "0")
        self.assertEqual(view.count_cell(3), "3")
        self.assertEqual(view.count_cell(None), "—")


if __name__ == "__main__":
    unittest.main()
