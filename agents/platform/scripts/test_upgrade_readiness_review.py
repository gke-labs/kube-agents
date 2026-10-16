"""Tests for upgrade_readiness_review.py: the mapping, the rubric, the registration, the record, the lines."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import findings_queue as fq  # noqa: E402
import upgrade_readiness_review as review  # noqa: E402
import upgrade_readiness_watch as watch  # noqa: E402

ROSTER = Path(__file__).resolve().parents[1] / "cron" / "jobs.json"
SOP = Path(__file__).resolve().parents[1] / "governance" / "upgrade_readiness_brief_sop.md"
NOW = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)
PROJECT = "acme-prod"
OTHER_PROJECT = "acme-dev"


def member(cluster: str, status: str = "ready", project: str = PROJECT, **readiness) -> dict:
    entry = {
        "project": project,
        "location": "us-central1-a",
        "cluster": cluster,
        "status": "lagging",
        "target_version": "1.36.4-gke.1391002",
        "readiness": {"status": status, "read_error": None, "webhook_read_error": None, **readiness},
    }
    return entry


def budget(name="pinned-batch-runner", namespace="seeded-upgrade", replicas=1) -> dict:
    return {
        "pdb": f"{namespace}/{name}",
        "namespace": namespace,
        "name": name,
        "field": "maxUnavailable: 0",
        "workloads": [{"kind": "Deployment", "namespace": namespace, "name": name, "replicas": replicas}],
        "expected_pods": replicas,
        "disruptions_allowed": 0,
    }


def seeded_b() -> dict:
    return member(
        "seeded-b",
        "blocked",
        pdbs={"blocking": [budget()], "evaluated": 1},
        maintenance={
            "exclusions": [
                {
                    "name": "hold-the-minor-lag",
                    "scope": "NO_MINOR_UPGRADES",
                    "end_time": "2027-01-05T18:27:10Z",
                    "in_effect": True,
                    "blocks": True,
                    "detail": "blocks auto-upgrade to 1.36.4-gke.1391002 until 2027-01-05T18:27Z",
                }
            ],
            "blocking_exclusions": ["hold-the-minor-lag"],
        },
        skew={
            "applicable": True,
            "pools": [
                {"name": "default-pool", "version": "1.34.12-gke.1011000", "minors_behind_target": 2, "verdict": "at ceiling", "detail": "2 minors behind the target control plane, at the skew ceiling"},
                {"name": "no-surge-pool", "version": "1.34.11-gke.1209000", "minors_behind_target": 2, "verdict": "at ceiling", "detail": "2 minors behind"},
            ],
        },
        webhooks={
            "blocking": [],
            "outage": [
                {
                    "webhook": "seeded-fail-closed-gate/gate.seeded.invalid",
                    "config_kind": "ValidatingWebhookConfiguration",
                    "config": "seeded-fail-closed-gate",
                    "name": "gate.seeded.invalid",
                    "service": "seeded-upgrade/nonexistent-admission-gate",
                    "reason": "Service seeded-upgrade/nonexistent-admission-gate does not exist",
                    "upgrade_path": [],
                    "rules": ["CREATE configmaps at v1"],
                    "live_rules": ["CREATE configmaps at v1"],
                }
            ],
        },
    )


def gemma() -> dict:
    return member(
        "gemma-gpu",
        "blocked",
        pdbs={"blocking": [budget("gemma-server", "kubeagents-system", 1)]},
        skew={"applicable": True, "pools": [{"name": "gpu-pool", "version": "1.31.14-gke.2704000", "minors_behind_target": 5, "verdict": "blocks", "detail": "5 minors behind"}]},
        webhooks={"blocking": [{"config_kind": "ValidatingWebhookConfiguration", "config": "gatekeeper", "name": "validation.gatekeeper.sh", "service": "gatekeeper-system/gatekeeper-webhook-service", "reason": "no ready endpoint", "upgrade_path": ["CREATE pods at v1"], "rules": ["CREATE pods at v1"]}], "outage": []},
    )


def with_rules() -> dict:
    return member(
        "seeded-a",
        "ready",
        rules={"surge-capacity": {"entry": 2, "can_block": True, "blocking": 1, "risks": 0}, "removed-node-label": {"entry": 12, "can_block": False, "blocking": 0, "risks": 1}},
        rule_blocking=[{"pool": "no-surge-pool", "strategy": "SURGE", "max_surge": 0, "text": "pool no-surge-pool (maxSurge 0, maxUnavailable 1; 1 node) has no room for its displaced pods"}],
        risks=[
            {"rule": "removed-node-label", "entry": 12, "grade": "risk", "kind": "Deployment", "namespace": "seeded-shapes", "name": "arch-pinned-worker", "object": "seeded-shapes/arch-pinned-worker", "text": "selects on beta.kubernetes.io/arch=amd64"},
            {"rule": "zonal-control-plane", "entry": 11, "text": "control plane is zonal (us-central1-a)"},
        ],
    )


def severity(item: dict) -> str:
    return review.severity_of(item)


class TheMapping(unittest.TestCase):
    def test_a_singleton_budget_is_critical_and_a_spread_one_is_major(self):
        single = review.budget_findings(seeded_b())[0]
        spread = review.budget_findings(member("c", "blocked", pdbs={"blocking": [budget(replicas=3)]}))[0]
        self.assertEqual(severity(single), "critical")
        self.assertEqual(severity(spread), "major")
        self.assertEqual(single["check"], "upgrade-drain-blocking-budget")
        self.assertEqual((single["namespace"], single["object"]), ("seeded-upgrade", "pinned-batch-runner"))
        self.assertIn("maxUnavailable to 1", single["recommendation"]["action"])

    def test_skew_blocks_is_critical_and_the_ceiling_is_major(self):
        ceiling = review.skew_findings(seeded_b())
        blocks = review.skew_findings(gemma())
        self.assertEqual([severity(f) for f in ceiling], ["major", "major"])
        self.assertEqual(severity(blocks[0]), "critical")
        self.assertEqual({f["object"] for f in ceiling}, {"default-pool", "no-surge-pool"})

    def test_a_webhook_in_the_upgrade_path_is_critical_and_an_outage_is_major(self):
        outage = review.webhook_findings(seeded_b())[0]
        blocking = review.webhook_findings(gemma())[0]
        self.assertEqual(severity(outage), "major")
        self.assertEqual(severity(blocking), "critical")
        self.assertEqual(outage["object"], "seeded-fail-closed-gate/gate.seeded.invalid")
        self.assertIn("in the upgrade's path", blocking["detail"])

    def test_an_exclusion_is_a_minor_posture(self):
        (exclusion,) = review.exclusion_findings(seeded_b())
        self.assertEqual(severity(exclusion), "minor")
        self.assertIn("2027-01-05", exclusion["title"])

    def test_rule_rows_map_by_entry_with_blockers_critical(self):
        found = review.rule_findings(with_rules())
        by_check = {f["check"]: f for f in found}
        self.assertEqual(set(by_check), {"upgrade-surge-capacity", "upgrade-removed-node-label", "upgrade-zonal-control-plane"})
        self.assertEqual(severity(by_check["upgrade-surge-capacity"]), "critical")
        self.assertEqual(by_check["upgrade-surge-capacity"]["object"], "no-surge-pool")
        self.assertEqual(severity(by_check["upgrade-removed-node-label"]), "major")
        self.assertEqual((by_check["upgrade-removed-node-label"]["namespace"], by_check["upgrade-removed-node-label"]["object"]), ("seeded-shapes", "Deployment/arch-pinned-worker"))
        self.assertEqual(severity(by_check["upgrade-zonal-control-plane"]), "minor")
        self.assertEqual(by_check["upgrade-zonal-control-plane"]["object"], review.CLUSTER_OBJECT)
        self.assertIn("Replace deprecated labels", by_check["upgrade-removed-node-label"]["recommendation"]["action"])

    def test_every_payload_is_one_the_queue_accepts(self):
        for m in (seeded_b(), gemma(), with_rules()):
            for item in review.member_findings(m):
                fq.validate_finding(review.payload(item))
                self.assertNotIn("_entry", review.payload(item))

    def test_ids_are_stable_and_short_enough_to_type(self):
        first = review.item_id(review.budget_findings(seeded_b())[0])
        second = review.item_id(review.budget_findings(seeded_b())[0])
        self.assertEqual(first, second)
        self.assertEqual(first, "upgrade-drain-blocking-budget.acme-prod.seeded-b.seeded-upgrade.pinned-batch-runner")

    def test_a_member_is_complete_only_when_every_read_landed(self):
        self.assertTrue(review.member_complete(seeded_b(), []))
        failed = seeded_b()
        failed["readiness"]["read_error"] = "connection refused"
        self.assertFalse(review.member_complete(failed, []))
        self.assertFalse(review.member_complete(seeded_b(), [{"project": PROJECT, "location": "us-central1-a", "cluster": "seeded-b", "error": "x"}]))

    def test_the_rubric_table_covers_the_queue_anchors(self):
        for values in list(review.RUBRIC.values()) + list(review.ENTRY_RISK_RUBRIC.values()) + [review.DEFAULT_RISK_RUBRIC]:
            fq.validate_rubric(review.rubric(values))


class Registration(unittest.TestCase):
    def test_batches_per_cluster_with_scope_only_when_complete(self):
        calls = []

        def fake_request(path, body):
            calls.append((path, body))
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]]}

        partial = gemma()
        partial["readiness"]["webhook_read_error"] = "timed out"
        members = [seeded_b(), partial]
        findings = [item for m in members for item in review.member_findings(m)]
        with mock.patch.object(review, "kv_request", fake_request):
            outcomes, problems, claimed = review.register(members, [], findings)
        self.assertEqual(problems, [])
        self.assertEqual(claimed, {(PROJECT, "seeded-b")})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["scope"], {"project": PROJECT, "cluster": "seeded-b", "complete": True, "check_prefix": review.CHECK_PREFIX})
        self.assertNotIn("scope", calls[1][1])
        self.assertEqual(set(outcomes.values()), {"created"})
        self.assertEqual(len(outcomes), len(findings))

    def test_a_complete_cluster_with_nothing_to_report_sends_an_empty_scoped_batch(self):
        calls = []

        def fake_request(path, body):
            calls.append(body)
            return {"results": []}

        clean = member("seeded-c")
        with mock.patch.object(review, "kv_request", fake_request):
            outcomes, problems, claimed = review.register([clean], [], [])
        self.assertEqual((outcomes, problems, claimed), ({}, [], {(PROJECT, "seeded-c")}))
        self.assertEqual(calls, [{"findings": [], "scope": {"project": PROJECT, "cluster": "seeded-c", "complete": True, "check_prefix": review.CHECK_PREFIX}}])
        partial = member("seeded-c")
        partial["readiness"]["read_error"] = "timed out"
        calls.clear()
        with mock.patch.object(review, "kv_request", fake_request):
            review.register([partial], [], [])
        self.assertEqual(calls, [])

    def test_a_cluster_past_the_batch_limit_is_sent_in_pieces_without_a_claim(self):
        calls = []

        def fake_request(path, body):
            calls.append(body)
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]]}

        big = member("seeded-z", "blocked", pdbs={"blocking": [budget(name=f"b{i}") for i in range(review.KV_BATCH_LIMIT + 1)]})
        findings = review.member_findings(big)
        with mock.patch.object(review, "kv_request", fake_request):
            outcomes, problems, claimed = review.register([big], [], findings)
        self.assertEqual(problems, [])
        self.assertEqual(claimed, set())
        self.assertEqual([len(c["findings"]) for c in calls], [review.KV_BATCH_LIMIT, 1])
        self.assertTrue(all("scope" not in c for c in calls))
        self.assertEqual(len(outcomes), review.KV_BATCH_LIMIT + 1)

    def test_a_failed_batch_is_reported_and_marks_its_rows(self):
        def failing(path, body):
            raise OSError("connection refused")

        members = [seeded_b()]
        findings = review.member_findings(members[0])
        with mock.patch.object(review, "kv_request", failing):
            outcomes, problems, _claimed = review.register(members, [], findings)
        self.assertEqual(len(problems), 1)
        self.assertIn("seeded-b", problems[0])
        self.assertEqual(set(outcomes.values()), {review.REGISTRATION_FAILED})


class TheRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "upgrade-readiness"
        self.env = mock.patch.dict(os.environ, {watch.WATCH_HOME_ENV: str(self.home), watch.PROJECTS_ENV: f"{PROJECT},{OTHER_PROJECT}"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def summary_for(self, members, outcomes=None, previous=None):
        findings = [item for m in members for item in review.member_findings(m)]
        outcomes = outcomes if outcomes is not None else {review.item_id(f): "created" for f in findings}
        doc = review.summary(stamp="20261018T060000Z", now=NOW, names=[PROJECT], members=members, errors=[], failures={}, findings=findings, outcomes=outcomes, problems=[], previous=previous, report_path=self.home / "x")
        return doc, findings

    def test_the_summary_counts_verdicts_severities_and_the_delta(self):
        doc, findings = self.summary_for([seeded_b(), gemma(), member("seeded-c")])
        self.assertEqual(doc["clusters"]["blocked"], [f"{PROJECT}/us-central1-a/gemma-gpu", f"{PROJECT}/us-central1-a/seeded-b"])
        self.assertEqual(doc["clusters"]["ready"], [f"{PROJECT}/us-central1-a/seeded-c"])
        self.assertEqual(doc["counts"]["critical"], 4)  # seeded-b's budget; gemma's budget, skew and webhook
        self.assertEqual(len(doc["new_ids"]), len(findings))
        # Last week's rows count as no longer reported only on a cluster this
        # run read; a cluster not read this week keeps its rows unjudged.
        gone = {"id": "upgrade-pool-skew.acme-prod.seeded-b._.old-pool", "project": PROJECT, "cluster": "seeded-b"}
        unread = {"id": "upgrade-pool-skew.acme-prod.old-cluster._.default-pool", "project": PROJECT, "cluster": "old-cluster"}
        half_read = {"id": "upgrade-pool-skew.acme-prod.gemma-gpu._.old-pool", "project": PROJECT, "cluster": "gemma-gpu"}
        kept = dict(doc["findings"][0])
        partial = gemma()
        partial["readiness"]["read_error"] = "timed out"
        doc2, _ = self.summary_for([seeded_b(), partial], previous={"findings": [gone, unread, half_read, kept]})
        self.assertEqual(doc2["no_longer_reported_ids"], [gone["id"]])

    def test_the_report_names_every_finding_with_its_id_and_action(self):
        doc, findings = self.summary_for([seeded_b()])
        text = review.render_report(doc, findings, {PROJECT: "| table |"}, [])
        for row in doc["findings"]:
            self.assertIn(f"`{row['id']}`", text)
            self.assertIn(row["action"], text)
        self.assertIn("| acme-prod/us-central1-a/seeded-b | blocked | 1 | 3 | 1 |", text)
        self.assertIn("## Report script output", text)
        self.assertIn("dismiss", text)

    def test_the_record_is_written_pruned_and_the_latest_files_refreshed(self):
        doc, findings = self.summary_for([seeded_b()])
        home = review.weekly_home()
        for stamp in [f"2026100{i}T060000Z" for i in range(1, 10)]:
            review.write_record(home, stamp, "# r\n", {"members": []}, {"findings": []}, doc)
        runs = sorted(p.name for p in home.iterdir() if p.is_dir())
        self.assertEqual(len(runs), review.KEEP_RUNS)
        self.assertEqual(runs[-1], "20261009T060000Z")
        self.assertEqual(json.loads((home / review.LATEST_JSON_NAME).read_text())["stamp"], doc["stamp"])
        self.assertEqual(review.load_previous(home)["stamp"], doc["stamp"])

    def test_the_sandbox_copy_program_writes_both_files_readable(self):
        program = review.sandbox_copy_program({"latest.md": "# r 🎄 exclusion\n", "latest.json": "{}"})
        self.assertIn(review.SANDBOX_COPY_DIR, program)
        self.assertIn("os.chmod", program)
        fake_dir = Path(self.tmp.name) / "sandbox"
        exec_globals = {}
        exec(program.replace(json.dumps(review.SANDBOX_COPY_DIR), json.dumps(str(fake_dir))), exec_globals)  # noqa: S102 - the program under test
        self.assertEqual(sorted(p.name for p in fake_dir.iterdir()), ["latest.json", "latest.md"])
        self.assertEqual((fake_dir / "latest.md").read_text(encoding="utf-8"), "# r 🎄 exclusion\n")
        self.assertEqual(oct(os.stat(fake_dir / "latest.md").st_mode & 0o777), oct(review.SANDBOX_COPY_MODE))
        # Read-only files in a read-only directory: a model turn's `cp` over the
        # copy fails instead of replacing the review's record.
        self.assertEqual(review.SANDBOX_COPY_MODE & 0o222, 0)
        self.assertEqual(oct(os.stat(fake_dir).st_mode & 0o777), oct(review.SANDBOX_COPY_DIR_CLOSED_MODE))
        with self.assertRaises(PermissionError):
            (fake_dir / "latest.md").write_text("clobbered")
        with self.assertRaises(PermissionError):
            (fake_dir / "fleet_versions.json").write_text("{}")
        # The next review replaces both files through the same program.
        second = review.sandbox_copy_program({"latest.md": "# second\n", "latest.json": "{\"n\": 2}"})
        exec(second.replace(json.dumps(review.SANDBOX_COPY_DIR), json.dumps(str(fake_dir))), {})  # noqa: S102 - the program under test
        self.assertEqual((fake_dir / "latest.md").read_text(encoding="utf-8"), "# second\n")
        self.assertEqual(oct(os.stat(fake_dir).st_mode & 0o777), oct(review.SANDBOX_COPY_DIR_CLOSED_MODE))
        os.chmod(fake_dir, review.SANDBOX_COPY_DIR_OPEN_MODE)  # so the fixture's cleanup can remove it

    def test_the_sop_reads_the_fields_the_summary_writes(self):
        # Step 1 of the brief SOP lists the summary's keys in backticks; a
        # renamed key here breaks every install's Saturday brief with no signal,
        # so the list is held to the dict `summary()` writes.
        import re
        doc, _ = self.summary_for([seeded_b()])
        text = SOP.read_text()
        step_one = text.split("### 1. Read the review", 1)[1].split("### 2.", 1)[0]
        named = set(re.findall(r"`([a-z_]+)`", step_one))
        row_keys = set(doc["findings"][0]) if doc["findings"] else set()
        self.assertTrue(named, "the SOP names no fields")
        for key in named - {"generated_at"}:
            self.assertTrue(key in doc or key in row_keys, f"the SOP names `{key}`, which the summary does not write")
        for key in ("generated_at", "projects", "clusters", "counts", "findings", "new_ids", "suppressed_ids", "no_longer_reported_ids", "project_failures", "registration_errors", "report_path"):
            self.assertIn(key, named, f"the SOP does not tell the brief about `{key}`")

    def test_projects_last_week_could_not_read_go_first(self):
        self.assertEqual(review.ordered_names(["a", "b", "c"], {"project_failures": {"c": "timed out"}}), ["c", "a", "b"])
        self.assertEqual(review.ordered_names(["a", "b"], None), ["a", "b"])


class TheTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "upgrade-readiness"
        self.env = mock.patch.dict(os.environ, {watch.WATCH_HOME_ENV: str(self.home), watch.PROJECTS_ENV: PROJECT, "SESSION_KV_API_KEY": "k"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.registered = []
        self.copies = []
        self.patches = [
            mock.patch.object(watch, "declared_scope", return_value=None),
            mock.patch.object(watch, "run_report", side_effect=self.fake_run_report),
            mock.patch.object(review, "kv_request", side_effect=self.fake_kv),
            mock.patch.object(review, "copy_to_sandbox", side_effect=self.fake_copy),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.envelope = {"exit": 0, "tables": "| t |", "report": {"members": [seeded_b(), member("seeded-c")], "errors": []}}

    def fake_run_report(self, names, readiness, *, scope, outside_stands, timeout=None):
        self.asked = (names, readiness, scope, timeout)
        return self.envelope

    def fake_kv(self, path, body):
        self.registered.append(body)
        return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]]}

    def fake_copy(self, report_md, summary_doc):
        self.copies.append((report_md, summary_doc))
        return None

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = review.main(list(argv))
        return code, out.getvalue()

    def test_a_clean_run_registers_writes_copies_and_says_nothing(self):
        code, out = self.run_main()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.asked[0], [PROJECT])
        self.assertTrue(self.asked[1])
        self.assertEqual(len(self.registered), 2)  # seeded-b's findings, and seeded-c's complete empty batch
        self.assertEqual(self.registered[0]["scope"]["cluster"], "seeded-b")
        self.assertEqual((self.registered[1]["findings"], self.registered[1]["scope"]["cluster"]), ([], "seeded-c"))
        home = review.weekly_home()
        self.assertTrue((home / review.LATEST_MD_NAME).exists())
        latest = json.loads((home / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["clusters"]["blocked"], [f"{PROJECT}/us-central1-a/seeded-b"])
        self.assertEqual(len(self.copies), 1)
        self.assertEqual(self.copies[0][1]["stamp"], latest["stamp"])

    def test_a_dry_run_registers_and_writes_nothing(self):
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(review.DRY_RUN_HEADER, out)
        self.assertIn("upgrade-drain-blocking-budget", out)
        self.assertEqual(self.registered, [])
        self.assertEqual(self.copies, [])
        self.assertFalse(review.weekly_home().exists())

    def test_a_failure_is_said_once_then_the_recovery(self):
        with mock.patch.object(watch, "run_report", side_effect=RuntimeError("sandbox exited 255 without a report")):
            code, out = self.run_main()
            self.assertEqual(code, 0)
            self.assertIn("failed: acme-prod: sandbox exited 255", out)
            code, out = self.run_main()
            self.assertEqual(out, "")
        code, out = self.run_main()
        self.assertEqual(out.strip(), review.RECOVERED_LINE.format(prefix=review.LOG_PREFIX))

    def test_a_project_not_read_is_one_line_and_the_rest_is_registered(self):
        calls = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            calls.append(names[0])
            if names[0] == OTHER_PROJECT:
                raise RuntimeError("the sandbox run timed out after 1500s")
            return self.envelope

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: f"{PROJECT},{OTHER_PROJECT}"}), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("1 project(s) not read this week: acme-dev: the sandbox run timed out", out)
        self.assertEqual(len(self.registered), 2)
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["project_failures"], {OTHER_PROJECT: "the sandbox run timed out after 1500s"})
        # Next run, the project that failed goes first.
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: f"{PROJECT},{OTHER_PROJECT}"}), mock.patch.object(watch, "run_report", side_effect=run_report):
            calls.clear()
            self.run_main()
        self.assertEqual(calls[0], OTHER_PROJECT)

    def test_each_project_is_registered_before_the_next_one_runs(self):
        events = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            events.append(("run", names[0]))
            return self.envelope

        def kv(path, body):
            events.append(("kv", body["scope"]["cluster"] if "scope" in body else body["findings"][0]["cluster"]))
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]]}

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: f"{PROJECT},{OTHER_PROJECT}"}), mock.patch.object(watch, "run_report", side_effect=run_report), mock.patch.object(review, "kv_request", side_effect=kv):
            self.run_main()
        runs = [i for i, e in enumerate(events) if e[0] == "run"]
        self.assertEqual(len(runs), 2)
        self.assertTrue(any(e[0] == "kv" for e in events[runs[0] : runs[1]]), events)

    def test_no_project_is_a_failure_line(self):
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ""}), mock.patch.object(watch, "projects", return_value=[]):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(review.NO_PROJECTS_DETAIL, out)


class TheRoster(unittest.TestCase):
    def test_the_two_saturday_jobs_are_on_the_roster_in_order(self):
        jobs = {job["id"]: job for job in json.loads(ROSTER.read_text())["jobs"]}
        review_job = jobs[review.JOB_ID]
        brief = jobs[review.BRIEF_JOB_ID]
        self.assertEqual(review_job["schedule"]["expr"], "0 6 * * 6")
        self.assertEqual(brief["schedule"]["expr"], "0 7 * * 6")
        self.assertTrue(review_job["no_agent"])
        self.assertEqual(review_job["script"], Path(review.__file__).name)
        self.assertEqual((review_job["deliver"], brief["deliver"]), ("chat", "chat"))
        self.assertIn("governance/upgrade_readiness_brief_sop.md", brief["prompt"])
        self.assertFalse(brief.get("no_agent"))

    def test_the_skill_carries_the_sop_shape_verbatim(self):
        # The on-request route lives in a skill the worker loads; its template
        # must be the SOP's, or a chat brief and a Saturday brief drift apart.
        skill = (Path(__file__).resolve().parents[1] / "skills" / "upgrade-readiness-brief" / "SKILL.md").read_text()
        def template(text):
            blocks, current = [], None
            for line in text.splitlines():
                if line.strip().startswith("```"):
                    if current is None:
                        current = []
                    else:
                        blocks.append(current)
                        current = None
                elif current is not None:
                    current.append(line.strip())
            shapes = [b for b in blocks if any("Decide now" in line for line in b)]
            self.assertEqual(len(shapes), 1, "one fenced template with Decide now")
            return "\n".join(line for line in shapes[0] if line)
        self.assertEqual(template(SOP.read_text()), template(skill))
        self.assertIn("upgrade-readiness-brief", SOP.read_text())

    def test_the_brief_sop_names_the_copy_the_review_places(self):
        text = SOP.read_text()
        self.assertIn(f"{review.SANDBOX_COPY_DIR}/{review.LATEST_JSON_NAME}", text)
        self.assertIn(f"{review.SANDBOX_COPY_DIR}/{review.LATEST_MD_NAME}", text)
        self.assertIn(f"`{review.CHECK_PREFIX}`", text)
        self.assertIn("get_ranked_findings", text)


if __name__ == "__main__":
    unittest.main()
