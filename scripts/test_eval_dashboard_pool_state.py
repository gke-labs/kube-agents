"""pool_state.py scans every pool project's shape and never fails the bot for
what it finds.

The scan runs the verifier per project and reads its --report; here the
verifier is a stub that answers from a JSON "world", so each test names only
its own defect. What is pinned:

* the checks the scan asks for are the verifier's read-only set and nothing
  that needs a GitHub credential, the fleet, or the checkout;
* a healthy project scans as every check `healthy`, and the document carries
  the scan time, the counts, and the previous scan's drift map;
* a failed check is `drifted` with its findings -- stable id, what was
  observed, the repair -- keyed by id; an unread check is `not_checked` with
  the verifier's warning; the verifier's exit code is never the verdict;
* a verifier that hangs or crashes without a report, and a missing gcloud,
  are each "not checked" with a reason, exit 0; a missing verifier is a
  repository bug, exit 1;
* the workflow wires the scan into the hourly job and the tick reads it.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

import verify_ci_pool_project as verifier
from eval_dashboard import pool_state

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci-health.yml"

UTC = timezone.utc
NOW = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)
PROJECT = "kube-agents-evals-2"
OTHER = "kube-agents-evals-3"
CHECKS = list(pool_state.DEFAULT_CHECKS)
FINDING = "iam/platform-gsa/missing/roles/serviceusage.serviceUsageConsumer"
REPAIR = 'gcloud projects add-iam-policy-binding kube-agents-evals-2 --member="serviceAccount:kubeagents-platform-gsa@kube-agents-evals-2.iam.gserviceaccount.com" --role=roles/serviceusage.serviceUsageConsumer'

# The verifier stub: `--report` is written from the world's entry for the
# project -- a report document, or a behaviour ("sleep", "crash", "silent").
_STUB_VERIFIER = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
def flag(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
    return None
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")
world = json.load(open(os.environ["STUB_WORLD"]))
project = flag("--project-id")
entry = world.get(project, "silent")
if entry == "sleep":
    time.sleep(30)
if entry == "crash":
    print("usage: something is wrong", file=sys.stderr)
    sys.exit(64)
if entry == "silent":
    sys.exit(1)
checks = flag("--checks").split(",")
doc = {"schema_version": 1, "project": project, "generated_at": "2026-09-27T20:00:00+00:00", "checks": {}}
for check in checks:
    record = entry.get(check) or {"status": "pass"}
    doc["checks"][check] = {"name": check, "status": record.get("status", "pass"), "message": record.get("message", ""),
                            "details": record.get("details", []), "warnings": record.get("warnings", []), "unread": record.get("unread", []), "findings": record.get("findings", [])}
with open(flag("--report"), "w") as fh:
    json.dump(doc, fh)
code = max((record.get("exit", 0) for record in entry.values() if isinstance(record, dict)), default=0)
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("exit %s %d\n" % (project, code))
sys.exit(code)
'''


def report(**checks):
    """A world entry: {check_id: {status, details, warnings, findings, exit}}."""
    return checks


def drifted(finding=FINDING, observed="The platform agent GSA is missing roles/serviceusage.serviceUsageConsumer on kube-agents-evals-2", repair=REPAIR):
    return {"status": "fail", "message": "IAM requirements missing", "details": [observed], "findings": [{"id": finding, "observed": observed, "repair": repair}], "exit": 1}


def unchecked(warning="Could not describe kube-agents-evals-2: PERMISSION_DENIED"):
    return {"status": "unchecked", "message": "Not checked", "warnings": [warning], "exit": 2}


class ScanHarness(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.stub = self.root / "verifier.py"
        self.stub.write_text(_STUB_VERIFIER, encoding="utf-8")
        self.log = self.root / "calls.log"
        self.log.write_text("")
        self.world = self.root / "world.json"
        self.workdir = self.root / "work"

    def scan(self, world: dict, projects=(PROJECT,), prior=None, timeout=20.0, which=None, checks=CHECKS):
        self.world.write_text(json.dumps(world), encoding="utf-8")
        environ = {**os.environ, "STUB_WORLD": str(self.world), "STUB_LOG": str(self.log)}
        return pool_state.scan(
            list(projects), checks, self.workdir, prior=prior, now=NOW, workers=2, project_timeout=timeout,
            which=which or (lambda name: "/usr/bin/gcloud"), verifier_script=self.stub, environ=environ,
        )

    def calls(self):
        return [line.split() for line in self.log.read_text().splitlines() if not line.startswith("exit ")]

    def exits(self):
        return [(parts[1], int(parts[2])) for parts in (line.split() for line in self.log.read_text().splitlines()) if parts[0] == "exit"]


class TheChecksItAsksFor(unittest.TestCase):
    def test_the_default_set_is_the_verifiers_read_only_set(self):
        self.assertEqual(pool_state.DEFAULT_CHECKS, tuple(verifier.POOL_STATE_CHECKS))
        for excluded in (verifier.CHECK_SEEDED_FLEET, verifier.CHECK_CODEBASE_MAPPING, verifier.CHECK_TOKEN_MINTER, *verifier.GITHUB_CHECKS):
            self.assertNotIn(excluded, pool_state.DEFAULT_CHECKS)
        self.assertIn(verifier.CHECK_TOKEN_MINTER_KMS, pool_state.DEFAULT_CHECKS)
        self.assertTrue(set(pool_state.DEFAULT_CHECKS) <= set(verifier.CHECK_IDS))


class OneProject(ScanHarness):
    def test_a_healthy_project_scans_as_every_check_healthy(self):
        doc = self.scan({PROJECT: report()})
        entry = doc["projects"][PROJECT]
        self.assertEqual({check: verdict["state"] for check, verdict in entry["checks"].items()}, {check: "healthy" for check in CHECKS})
        self.assertEqual(entry["findings"], {})
        self.assertEqual(entry["summary"], {"healthy": len(CHECKS), "drifted": 0, "not_checked": 0})
        self.assertNotIn("error", entry)
        self.assertEqual(doc["summary"], {"projects": 1, "checked": 1, "drifted_projects": 0, "findings": 0, "healthy": len(CHECKS), "drifted": 0, "not_checked": 0})
        self.assertEqual((doc["schema_version"], doc["scanned_at"], doc["checks"]), (1, pool_state.iso(NOW), CHECKS))
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--project-id") + 1], PROJECT)
        self.assertEqual(argv[argv.index("--checks") + 1], ",".join(CHECKS))
        self.assertEqual(argv[argv.index("--location") + 1], pool_state.DEFAULT_LOCATION)

    def test_a_finding_is_drifted_with_its_id_observation_and_repair(self):
        doc = self.scan({PROJECT: report(iam=drifted())})
        entry = doc["projects"][PROJECT]
        self.assertEqual(entry["checks"]["iam"]["state"], "drifted")
        self.assertEqual(entry["checks"]["iam"]["detail"], ["The platform agent GSA is missing roles/serviceusage.serviceUsageConsumer on kube-agents-evals-2"])
        self.assertEqual(entry["findings"], {FINDING: {"check": "iam", "detail": ["The platform agent GSA is missing roles/serviceusage.serviceUsageConsumer on kube-agents-evals-2"], "repair": REPAIR}})
        self.assertEqual(pool_state.drift_map(doc), {PROJECT: [FINDING]})
        self.assertEqual(pool_state.read_map(doc), {PROJECT: sorted(CHECKS)})
        self.assertEqual(pool_state.check_of(doc, PROJECT, FINDING), "iam")
        self.assertEqual(pool_state.repair_for(doc, PROJECT, FINDING), REPAIR)
        self.assertEqual(doc["summary"]["findings"], 1)
        self.assertEqual(doc["summary"]["drifted_projects"], 1)

    def test_the_stub_exits_the_way_the_verifier_does(self):
        # The fixtures' exit codes sit on the check records; the stub must
        # exit with them or the tests below never see a non-zero verifier.
        self.scan({PROJECT: report(iam=drifted())})
        self.assertIn(("kube-agents-evals-2", 1), self.exits())
        self.scan({OTHER: report(iam=unchecked())}, projects=(OTHER,))
        self.assertIn(("kube-agents-evals-3", 2), self.exits())

    def test_the_verifiers_exit_code_is_not_the_verdict(self):
        # Exit 1 on a finding, 2 on an unread item: the report says which check,
        # and the scan reads that, not the code.
        doc = self.scan({PROJECT: report(iam=drifted(), gke_and_state=unchecked("Could not list the clusters"))})
        entry = doc["projects"][PROJECT]
        self.assertEqual((entry["checks"]["iam"]["state"], entry["checks"]["gke_and_state"]["state"], entry["checks"]["artifact_registry"]["state"]), ("drifted", "not_checked", "healthy"))
        self.assertEqual(entry["checks"]["gke_and_state"]["detail"], ["Could not list the clusters"])
        self.assertNotIn("error", entry)
        self.assertEqual(pool_state.read_map(doc)[PROJECT], sorted(set(CHECKS) - {"gke_and_state"}))

    def test_a_check_read_in_part_counts_as_checked_but_not_as_read(self):
        # A pass with a warning read some of the item: the project was seen
        # (not a blind scan), but a finding in the refused read was not, so the
        # exit may not take the check as proof the finding is gone.
        partial = {"status": "pass", "message": "the Workload Identity binding verified; the project roles not checked", "warnings": ["Could not read the project IAM policy"], "unread": ["Could not read the project IAM policy"], "exit": 2}
        doc = self.scan({PROJECT: report(iam=partial)})
        self.assertEqual(doc["projects"][PROJECT]["checks"]["iam"], {"state": "healthy", "detail": ["Could not read the project IAM policy"], "unread": ["Could not read the project IAM policy"]})
        self.assertEqual(pool_state.read_map(doc)[PROJECT], sorted(set(CHECKS) - {"iam"}))
        self.assertEqual(pool_state.checked_projects(doc), 1)

    def test_a_drifted_check_with_a_refused_read_is_not_read_in_full(self):
        # A missing binding read off one policy beside a 429 on another: the
        # finding is real, and the refused read may hide the incident's.
        record = dict(drifted(), warnings=["Could not read the project IAM policy: 429"], unread=["Could not read the project IAM policy: 429"])
        doc = self.scan({PROJECT: report(iam=record)})
        self.assertEqual(doc["projects"][PROJECT]["checks"]["iam"]["state"], "drifted")
        self.assertEqual(doc["projects"][PROJECT]["checks"]["iam"]["unread"], ["Could not read the project IAM policy: 429"])
        self.assertEqual(pool_state.read_map(doc)[PROJECT], sorted(set(CHECKS) - {"iam"}))
        self.assertIn(FINDING, doc["projects"][PROJECT]["findings"])

    def test_unread_checks_on_checked_projects_are_counted(self):
        # One check read, four not: the project counts as checked, and the
        # four are what keeps the digest from calling it clean.
        world = {check: unchecked("429") for check in CHECKS}
        world["project_and_apis"] = {"status": "pass", "message": "ok", "exit": 0}
        doc = self.scan({PROJECT: report(**world)})
        self.assertEqual(pool_state.checked_projects(doc), 1)
        self.assertEqual(pool_state.unread_units(doc), len(CHECKS) - 1)
        self.assertEqual(pool_state.unread_units(self.scan({PROJECT: report()})), 0)
        blind = self.scan({PROJECT: report(**{check: unchecked() for check in CHECKS})})
        self.assertEqual(pool_state.unread_units(blind), 0, "an unchecked project is blind, not partial")
        # A check read in part (healthy, with a refused read) counts too: the
        # bucket describe refused on a project whose clusters were listed.
        partial = {"status": "pass", "message": "clusters present; state bucket not checked", "warnings": ["Could not read gs://p-tf-state"], "unread": ["Could not read gs://p-tf-state"], "exit": 2}
        doc = self.scan({PROJECT: report(gke_and_state=partial)})
        self.assertEqual(doc["projects"][PROJECT]["checks"]["gke_and_state"]["state"], "healthy")
        self.assertEqual(pool_state.unread_units(doc), 1)

    def test_advice_on_a_read_that_happened_still_counts_as_read_in_full(self):
        # The minter check warns about a second ENABLED key version on a
        # project it read whole; the exit may still take it as proof.
        advised = {"status": "pass", "message": "Minter provisioned", "warnings": ["KMS key k has 2 ENABLED versions; disable the others"], "unread": [], "exit": 0}
        doc = self.scan({PROJECT: report(token_minter_kms=advised)})
        self.assertEqual(doc["projects"][PROJECT]["checks"]["token_minter_kms"], {"state": "healthy", "detail": ["KMS key k has 2 ENABLED versions; disable the others"], "unread": []})
        self.assertEqual(pool_state.read_map(doc)[PROJECT], sorted(CHECKS))

    def test_a_project_the_bot_cannot_read_at_all_is_unread(self):
        doc = self.scan({PROJECT: report(**{check: unchecked() for check in CHECKS})})
        self.assertEqual(pool_state.read_map(doc), {})
        self.assertEqual(pool_state.checked_projects(doc), 0)
        self.assertEqual(pool_state.not_checked_reason(doc), "Could not describe kube-agents-evals-2: PERMISSION_DENIED")
        self.assertEqual(doc["summary"]["checked"], 0)
        # The verifier's real shape: the project check names the refusal and
        # the two checks that need its number restate it in their own words;
        # the reason quoted is the project check's, not the commonest line.
        world = {check: unchecked("Not checked: kube-agents-evals-2's project number could not be read (Could not describe kube-agents-evals-2: PERMISSION_DENIED)") for check in CHECKS}
        world["project_and_apis"] = unchecked("Could not describe kube-agents-evals-2, so neither it nor anything derived from its project number was checked: PERMISSION_DENIED")
        doc = self.scan({PROJECT: report(**world)})
        self.assertTrue(pool_state.not_checked_reason(doc).startswith("Could not describe kube-agents-evals-2, so neither"))
        # Mixed causes: one project refused at the describe, the others at the
        # ceiling; the reason is the one most projects share, not the refusal.
        stalled = {p: "sleep" for p in ("kube-agents-evals-4", "kube-agents-evals-5", "kube-agents-evals-6")}
        mixed = self.scan({PROJECT: report(**world), **stalled}, projects=(PROJECT, *stalled), timeout=0.5)
        self.assertTrue(pool_state.not_checked_reason(mixed).startswith("scripts/verify_ci_pool_project.py did not finish within"), pool_state.not_checked_reason(mixed))

    def test_a_check_the_report_omits_is_not_checked(self):
        checks_out, findings = pool_state.from_report({"checks": {"iam": {"status": "pass"}}}, ["iam", "gke_and_state"])
        self.assertEqual(checks_out["gke_and_state"], {"state": "not_checked", "detail": [pool_state.REASON_NO_REPORT]})
        self.assertEqual((checks_out["iam"]["state"], findings), ("healthy", {}))


class WhatNeverFailsTheBot(ScanHarness):
    def test_a_verifier_that_hangs_is_not_checked_with_the_ceiling(self):
        doc = self.scan({PROJECT: "sleep"}, timeout=1.0)
        entry = doc["projects"][PROJECT]
        self.assertEqual(entry["error"], pool_state.REASON_VERIFIER_TIMEOUT.format(seconds=1))
        self.assertTrue(all(verdict["state"] == "not_checked" for verdict in entry["checks"].values()))

    def test_the_verifier_gets_a_deadline_inside_the_scans_ceiling(self):
        # A stall must not cost the whole report: past the deadline the
        # verifier starts no check and cuts every command short, 30 s inside
        # the scan's ceiling.
        self.assertEqual(pool_state.verifier_deadline(300), 270)
        self.assertEqual(pool_state.verifier_deadline(20), 1)
        for bad in (float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                pool_state.verifier_deadline(bad)
        self.scan({PROJECT: report()})
        argv = self.calls()[0]
        self.assertIn("--deadline-seconds", argv)
        self.assertEqual(argv[argv.index("--deadline-seconds") + 1], "1")

    def test_a_report_left_by_an_earlier_run_is_not_read_as_this_runs(self):
        # The same --workdir twice: a healthy first run, then a verifier that
        # dies before writing. The old file must not become this run's verdict.
        first = self.scan({PROJECT: report()})
        self.assertEqual(first["projects"][PROJECT]["checks"]["iam"]["state"], "healthy")
        second = self.scan({PROJECT: "crash"})
        self.assertEqual(second["projects"][PROJECT]["checks"]["iam"]["state"], "not_checked")
        self.assertIn("exited 64 without a report", second["projects"][PROJECT]["error"])

    def test_a_verifier_that_dies_without_a_report_is_not_checked_with_its_words(self):
        doc = self.scan({PROJECT: "crash"})
        self.assertEqual(doc["projects"][PROJECT]["error"], "scripts/verify_ci_pool_project.py exited 64 without a report: usage: something is wrong")
        # Exit 1 is the verifier's own "a finding" code, but with no report it
        # is a crash (an uncaught exception exits 1), and stderr is kept.
        quiet = self.scan({OTHER: "silent"}, projects=(OTHER,))
        self.assertEqual(quiet["projects"][OTHER]["error"], "scripts/verify_ci_pool_project.py exited 1 without a report: no output")

    def test_a_missing_gcloud_is_not_checked_everywhere_and_the_verifier_never_runs(self):
        doc = self.scan({PROJECT: report()}, which=lambda name: None)
        self.assertEqual(doc["projects"][PROJECT]["error"], "gcloud is not on PATH, so nothing was checked")
        self.assertEqual(self.calls(), [])

    def test_the_other_projects_go_on_when_one_fails(self):
        doc = self.scan({PROJECT: "crash", OTHER: report()}, projects=(PROJECT, OTHER))
        self.assertEqual((doc["projects"][PROJECT].get("error") is not None, doc["projects"][OTHER].get("error")), (True, None))
        self.assertEqual(doc["summary"]["checked"], 1)


class TheDocument(ScanHarness):
    def test_the_previous_scans_drift_rides_along(self):
        prior = self.scan({PROJECT: report(iam=drifted())})
        doc = self.scan({PROJECT: report()}, prior=prior)
        self.assertEqual(doc["previous"], {"scanned_at": pool_state.iso(NOW), "drifted": {PROJECT: [FINDING]}})
        self.assertEqual(pool_state.previous_drift_map(doc), {PROJECT: [FINDING]})
        self.assertEqual(pool_state.drift_map(doc), {})
        none = self.scan({PROJECT: report()}, prior=None)
        self.assertEqual(none["previous"], {"scanned_at": None, "drifted": {}})


class EntryPoint(ScanHarness):
    def _run(self, *args):
        stderr = __import__("io").StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            rc = pool_state.main(list(args))
        return rc, stderr.getvalue()

    def test_a_checkout_without_the_mapping_is_a_repository_bug(self):
        deploy = self.root / "ci-deploy.sh"
        deploy.write_text("#!/bin/bash\necho no mapping here\n", encoding="utf-8")
        rc, err = self._run("--out", str(self.root / "out.json"), "--ci-deploy-script", str(deploy), "--verifier", str(self.stub))
        self.assertEqual(rc, pool_state.EXIT_REPOSITORY_BUG)
        self.assertIn("gitops_repo_for_project", err)

    def test_a_missing_verifier_is_a_repository_bug(self):
        rc, err = self._run("--out", str(self.root / "out.json"), "--projects", PROJECT, "--verifier", str(self.root / "nowhere.py"))
        self.assertEqual(rc, pool_state.EXIT_REPOSITORY_BUG)
        self.assertIn("nowhere.py", err)

    def test_projects_are_stripped_of_whitespace_around_the_separator(self):
        self.world.write_text(json.dumps({PROJECT: report(), OTHER: report()}), encoding="utf-8")
        out = self.root / "pool-state.json"
        with unittest.mock.patch.dict(os.environ, {"STUB_WORLD": str(self.world), "STUB_LOG": str(self.log)}), \
             unittest.mock.patch.object(pool_state, "missing_binaries", return_value=[]):
            rc, _ = self._run("--out", str(out), "--projects", f"{PROJECT}, {OTHER} ", "--verifier", str(self.stub), "--now", NOW.isoformat(), "--workdir", str(self.workdir))
        self.assertEqual(rc, pool_state.EXIT_OK)
        self.assertEqual(sorted(json.loads(out.read_text(encoding="utf-8"))["projects"]), sorted([PROJECT, OTHER]))

    def test_a_project_id_with_a_path_in_it_is_refused_at_the_door(self):
        # An id is a directory name under the work directory: nothing with a
        # path in it, or outside the mapping's shape, reaches the scan.
        for bad in ("../evals-2", "/tmp/x", "kube-agents-evals-2/..", "Evals-2", "evals 2", "a" * 31):
            stderr = __import__("io").StringIO()
            with unittest.mock.patch("sys.stderr", stderr), self.assertRaises(SystemExit) as raised:
                pool_state.main(["--out", str(self.root / "out.json"), "--projects", f"{PROJECT},{bad}", "--verifier", str(self.stub), "--workdir", str(self.workdir)])
            self.assertEqual(raised.exception.code, 2, bad)
            self.assertIn("not a project id", stderr.getvalue())
            self.assertIn(bad, stderr.getvalue())
        self.assertFalse(self.workdir.exists(), "nothing was created under the work directory")
        self.assertFalse((self.workdir.parent / "evals-2").exists(), "nothing was created beside it")
        self.assertTrue(pool_state.PROJECT_ID_RE.match("kube-agents-evals-35"))

    def test_a_report_the_scan_cannot_read_says_so_rather_than_no_report(self):
        # A verifier cut off mid-write leaves a report that is there and not
        # JSON; the reason names that, not a crash with no report.
        def cut_short(argv, **kwargs):
            report = pathlib.Path(argv[argv.index("--report") + 1])
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text('{"checks": {"iam": {"status": "pa', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "iam: pass")
        entry = pool_state.scan_project(PROJECT, ["iam"], self.root / "work", 5.0, verifier_script=self.stub, runner=cut_short)
        self.assertTrue(entry["error"].startswith("scripts/verify_ci_pool_project.py exited 0 and its report could not be read:"), entry["error"])
        self.assertEqual(entry["checks"]["iam"]["state"], "not_checked")

        def not_an_object(argv, **kwargs):
            report = pathlib.Path(argv[argv.index("--report") + 1])
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("[]", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        entry = pool_state.scan_project(PROJECT, ["iam"], self.root / "work", 5.0, verifier_script=self.stub, runner=not_an_object)
        self.assertIn("not an object", entry["error"])

    def test_the_failed_finding_suffix_is_the_verifiers(self):
        # health.py holds a `<check>/failed` unit while its check has any
        # finding; the suffix it looks for is the id the verifier synthesises.
        from eval_dashboard import health
        self.assertEqual(health.SCAN_FAILED_SUFFIX, "/" + verifier.REPORT_FINDING_FAILED)

    def test_a_work_directory_that_cannot_be_made_is_not_checked_not_a_dead_scan(self):
        # scan_project's contract holds for its own filesystem calls too: a
        # work directory it cannot prepare (here, its parent is a file) is
        # that project's reason, and the scan over the others goes on.
        not_a_dir = self.root / "not-a-dir"
        not_a_dir.write_text("", encoding="utf-8")
        entry = pool_state.scan_project(PROJECT, ["iam", "gke_and_state"], not_a_dir, 5.0, verifier_script=self.stub)
        self.assertTrue(entry["error"].startswith("the scan could not prepare a work directory"), entry["error"])
        self.assertEqual({c["state"] for c in entry["checks"].values()}, {"not_checked"})
        self.assertEqual(entry["findings"], {})

    def test_a_scalar_where_a_report_lists_lines_is_a_thin_verdict_not_a_dead_scan(self):
        record = {"status": "fail", "message": "IAM requirements missing", "details": 5, "warnings": True, "unread": 0, "findings": 1}
        checks_out, findings = pool_state.from_report({"checks": {"iam": record, "gke_and_state": {"status": "pass", "warnings": "one", "unread": 2}}}, ["iam", "gke_and_state"])
        self.assertEqual(checks_out["iam"], {"state": "drifted", "detail": ["IAM requirements missing"], "unread": []})
        self.assertEqual(checks_out["gke_and_state"], {"state": "healthy", "detail": [], "unread": []})
        self.assertEqual(findings, {})

    def test_a_bad_project_timeout_is_refused_at_the_door(self):
        for bad in ("nan", "inf", "-1", "soon"):
            stderr = __import__("io").StringIO()
            with unittest.mock.patch("sys.stderr", stderr), self.assertRaises(SystemExit) as raised:
                pool_state.main(["--out", str(self.root / "out.json"), "--projects", PROJECT, "--verifier", str(self.stub), "--project-timeout", bad])
            self.assertEqual(raised.exception.code, 2, bad)
            self.assertIn("seconds", stderr.getvalue())

    def test_an_unknown_check_is_a_repository_bug(self):
        rc, err = self._run("--out", str(self.root / "out.json"), "--projects", PROJECT, "--verifier", str(self.stub), "--checks", "iam,no_such_check")
        self.assertEqual(rc, pool_state.EXIT_REPOSITORY_BUG)
        self.assertIn("no_such_check", err)

    def test_main_writes_the_document_and_exits_zero_on_findings(self):
        self.world.write_text(json.dumps({PROJECT: report(iam=drifted())}), encoding="utf-8")
        out = self.root / "pool-state.json"
        with unittest.mock.patch.dict(os.environ, {"STUB_WORLD": str(self.world), "STUB_LOG": str(self.log)}), \
             unittest.mock.patch.object(pool_state, "missing_binaries", return_value=[]):
            rc, err = self._run("--out", str(out), "--projects", PROJECT, "--verifier", str(self.stub), "--now", NOW.isoformat(), "--workdir", str(self.workdir))
        self.assertEqual(rc, pool_state.EXIT_OK, err)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(pool_state.drift_map(doc), {PROJECT: [FINDING]})
        self.assertIn("1 of 1 pool projects checked, 1 with drift (1 findings", err)


class Workflow(unittest.TestCase):
    """ci-health.yml wires the scan the way docs/ci-health.md says: into the
    hourly job after the fleet scan, with its own document and knobs, and the
    tick reads it."""

    def setUp(self):
        import yaml

        self.text = WORKFLOW.read_text()
        self.doc = yaml.safe_load(self.text)
        self.jobs = self.doc["jobs"]

    def test_the_scan_runs_in_the_hourly_job_after_the_fleet_scan(self):
        steps = self.jobs["fixture-state-scan"]["steps"]
        names = [step.get("name", "") for step in steps]
        fleet = next(i for i, step in enumerate(steps) if "fixture_state.py" in step.get("run", ""))
        scan = next(i for i, step in enumerate(steps) if "pool_state.py" in step.get("run", ""))
        self.assertLess(fleet, scan)
        run = steps[scan]["run"]
        self.assertIn("--prior work/pool-state-prior.json", run)
        self.assertIn('--workers "$POOL_STATE_WORKERS"', run)
        self.assertIn('--project-timeout "$POOL_STATE_PROJECT_TIMEOUT_S"', run)
        self.assertIn('timeout "$POOL_STATE_TIMEOUT_S"', run)
        self.assertEqual(steps[fleet].get("id"), "fleet_scan")
        fleet_upload = next(step for step in steps if "cp work/fixture-state.json" in step.get("run", ""))
        self.assertEqual(fleet_upload.get("id"), "fleet_upload")
        for step in (steps[scan], steps[scan - 1], steps[scan + 1]):
            # The failures the pool steps run through are the fleet scan's and
            # its upload's; `always()` or `!cancelled()` would also run them
            # after an auth or setup failure, with no credential and no work
            # directory.
            self.assertEqual(step.get("if"), "${{ success() || steps.fleet_scan.outcome == 'failure' || steps.fleet_upload.outcome == 'failure' }}", f"{step.get('name')} must run whether or not the fleet scan or its upload failed, and after nothing else's")
        upload = next(step for step in steps if "cp work/pool-state.json" in step.get("run", ""))
        self.assertIn('"$DASHBOARD_BUCKET/pool-state.json"', upload["run"])
        self.assertIn("Fetch the previous pool-state scan", names)
        env = self.doc["env"]
        self.assertTrue(int(env["POOL_STATE_TIMEOUT_S"]) > int(env["POOL_STATE_PROJECT_TIMEOUT_S"]) > 0)

    def test_each_scans_ceiling_covers_every_wave_of_the_mapped_pool(self):
        # A pool-wide API stall puts every project at the per-project ceiling;
        # the scan ceiling must outlast ceil(projects / workers) such waves or
        # the step is killed and publishes nothing. The job's clock covers both.
        import math

        env = self.doc["env"]
        mapped = len(pool_state.pool_projects(pool_state.CI_DEPLOY_SCRIPT.read_text()))
        self.assertGreaterEqual(mapped, 1, "the mapping was read")
        # The fleet scan mints a token (up to IMPERSONATE_TIMEOUT_S) before
        # its per-project deadline starts; the pool scan has no such step.
        mint = {"FIXTURE_STATE": pool_state.fixture_state.IMPERSONATE_TIMEOUT_S, "POOL_STATE": 0}
        for prefix in ("FIXTURE_STATE", "POOL_STATE"):
            waves = math.ceil(mapped / int(env[f"{prefix}_WORKERS"]))
            self.assertGreater(int(env[f"{prefix}_TIMEOUT_S"]), waves * (int(env[f"{prefix}_PROJECT_TIMEOUT_S"]) + mint[prefix]), prefix)
        both = int(env["FIXTURE_STATE_TIMEOUT_S"]) + int(env["POOL_STATE_TIMEOUT_S"])
        self.assertGreater(self.jobs["fixture-state-scan"]["timeout-minutes"] * 60, both + 600, "setup and the uploads need their ten minutes")
        self.assertGreaterEqual(self.jobs["fixture-state-scan"]["timeout-minutes"] * 60, int(env["FIXTURE_STATE_TIMEOUT_S"]) + int(env["POOL_STATE_TIMEOUT_S"]))

    def test_the_tick_reads_the_published_scan(self):
        steps = self.jobs["refresh-and-adjudicate"]["steps"]
        fetch = next(step for step in steps if "pool-state.json" in step.get("run", "") and "health-prev.json" in step["run"])
        self.assertIn('gsutil -q cp "$DASHBOARD_BUCKET/pool-state.json" work/pool-state.json || true', fetch["run"])
        adjudicate = next(step for step in steps if step.get("name") == "Adjudicate")
        self.assertIn("--pool-state work/pool-state.json", adjudicate["run"])

    def test_the_fleet_upload_steps_summary_script_runs_through_the_shell(self):
        # The same `python3 -c '…'` shape as the pool step, and the hourly
        # job was red on it: a quote inside the script ended the shell word.
        steps = self.jobs["fixture-state-scan"]["steps"]
        upload = next(step for step in steps if "cp work/fixture-state.json" in step.get("run", ""))
        run = upload["run"]
        start = run.index("python3 -c '")
        end = run.index("\n'", start) + 2
        shell_word = run[start:end].replace("python3 ", f"'{sys.executable}' ", 1)
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp) / "work"
            work.mkdir()
            document = {
                "summary": {"checked": 1, "projects": 2, "drifted_projects": 1, "healthy": 3, "drifted": 1, "not_checked": 4},
                "projects": {
                    "p1": {"roles": {"crashloop-workload": {"state": "drifted"}}},
                    "p2": {"roles": {"crashloop-workload": {"state": "not_checked"}}, "error": "cannot read the project"},
                },
            }
            (work / "fixture-state.json").write_text(json.dumps(document), encoding="utf-8")
            proc = subprocess.run(["bash", "-c", shell_word], cwd=tmp, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 of 2 projects checked; drifted on", proc.stdout)
        self.assertIn("p1: drifted: crashloop-workload", proc.stdout)
        self.assertIn("p2: not checked: cannot read the project", proc.stdout)

    def test_the_upload_steps_summary_script_runs(self):
        steps = self.jobs["fixture-state-scan"]["steps"]
        upload = next(step for step in steps if "cp work/pool-state.json" in step.get("run", ""))
        run = upload["run"]
        # The whole `python3 -c '…'` word, run through the shell as the step
        # does, so a quote inside the script that would end the word is caught.
        start = run.index("python3 -c '")
        end = run.index("\n'", start) + 2
        shell_word = run[start:end].replace("python3 ", f"'{sys.executable}' ", 1)
        code = run[start + len("python3 -c '") : end - 2]
        compile(code, "<upload step>", "exec")
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp) / "work"
            work.mkdir()
            document = {
                "summary": {"checked": 1, "projects": 2, "drifted_projects": 1, "findings": 1, "healthy": 4, "drifted": 1, "not_checked": 5},
                "projects": {
                    "p1": {"checks": {"iam": {"state": "drifted"}}, "findings": {FINDING: {"check": "iam"}}},
                    "p2": {"checks": {"iam": {"state": "not_checked"}}, "findings": {}, "error": "cannot read the project"},
                },
            }
            (work / "pool-state.json").write_text(json.dumps(document), encoding="utf-8")
            proc = subprocess.run(["bash", "-c", shell_word], cwd=tmp, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 of 2 projects checked; drifted on 1 (1 findings)", proc.stdout)
        self.assertIn(f"p1: drifted: {FINDING}", proc.stdout)
        self.assertIn("p2: not checked: cannot read the project", proc.stdout)


if __name__ == "__main__":
    unittest.main()
