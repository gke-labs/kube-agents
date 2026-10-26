# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The ``report_store_reply`` verifier, over a report store in a temporary directory.

The store is written by ``audit_report.write_report`` and read for real through ``sh -c``
with the repository's ``report_status.py`` as the reader; only the two pod shells, the reader's
path and the store root are swapped for local ones.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import onboarding, report_store_reply, transcript
from kube_agents_bench.report_store_reply import ReportStoreReplyVerifier, cites, mentioned_audits

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
TASK = REPO / "bench" / "tasks" / "fleet-audit-request-line" / "task.yaml"
OWNER_REPO = "acme/gitops"
COMPLIANCE = "compliance-audit"
COST = "fleet-wide-cost-analysis"
STARTED = time.time() - 3600
BEFORE = STARTED - 7200
DURING = STARTED + 600
URL_COMPLIANCE = "https://github.com/acme/gitops/issues/12"
URL_COMPLIANCE_NEW = "https://github.com/acme/gitops/issues/31"
URL_COST = "https://github.com/acme/gitops/issues/7"


def _audit_report():
    spec = importlib.util.spec_from_file_location("audit_report_for_reply_test", SCRIPTS / "audit_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUDIT_REPORT = _audit_report()


def _local_shell(script: str, timeout: float) -> str:
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def _silent_shell(script: str, timeout: float) -> str:
    return ""


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = tmp_path / "reports"
    monkeypatch.setenv("FLEET_AUDIT_REPORTS_DIR", str(store))
    monkeypatch.setattr(report_store_reply, "REPORT_STORE_ROOT", str(store))
    monkeypatch.setattr(report_store_reply, "READER_PATH", str(SCRIPTS / "report_status.py"))
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)
    monkeypatch.setattr(onboarding, "agent_shell", _local_shell)
    yield store
    transcript.clear()


def _write(audit: str, *, issue_url: str | None, issue_number: int | None, written: float,
           status: str = "UPDATED", repo: str = OWNER_REPO) -> None:
    now = datetime.fromtimestamp(written, tz=timezone.utc)
    envelope = AUDIT_REPORT.report_envelope(
        audit, {"status": status, "issue_url": issue_url}, {"findings": []}, now,
        repo=repo, issue_number=issue_number, ledger_body="", new_ids=[], resolved_ids=[], rendered_ids=[],
    )
    assert AUDIT_REPORT.write_report(audit, envelope, now)
    directory = AUDIT_REPORT.reports_dir_for(audit, repo)
    stamp = now.strftime(AUDIT_REPORT.REPORT_STAMP_FORMAT)
    for path in (directory / "runs" / f"{stamp}.json", directory / "latest.json"):
        os.utime(path, (written, written))


def _verify(reply: str, audits=(COMPLIANCE, COST)):
    transcript.set(output=reply, final_message=reply, trajectory=[], started_at=STARTED)
    return ReportStoreReplyVerifier(type="report_store_reply", audits=list(audits)).verify(1.0)


def test_each_audits_ledger_url_passes(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    _write(COST, issue_url=URL_COST, issue_number=7, written=BEFORE)
    result = _verify(f"compliance-audit: UPDATED, ledger {URL_COMPLIANCE}\nfleet-wide-cost-analysis: {URL_COST}.")
    assert result.status == "pass", result.reason


def test_a_missing_url_fails_that_audit_only(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    _write(COST, issue_url=URL_COST, issue_number=7, written=BEFORE)
    result = _verify(f"compliance-audit: 3 critical.\nfleet-wide-cost-analysis: {URL_COST}")
    assert result.status == "fail"
    assert "compliance-audit: no ledger URL" in result.reason
    assert "fleet-wide-cost-analysis" not in result.reason


def test_another_audits_url_does_not_stand_in_for_its_own(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    _write(COST, issue_url=URL_COST, issue_number=7, written=BEFORE)
    result = _verify(f"compliance-audit: {URL_COST}\nfleet-wide-cost-analysis: see above.")
    assert result.status == "fail"
    assert "compliance-audit: no ledger URL" in result.reason
    assert "fleet-wide-cost-analysis" not in result.reason


def test_a_url_counts_wherever_the_reply_puts_it(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    _write(COST, issue_url=URL_COST, issue_number=7, written=BEFORE)
    reply = (
        "Reports for compliance-audit and fleet-wide-cost-analysis:\n"
        f"| Security | {URL_COMPLIANCE} |\n| Cost | {URL_COST} |\n"
    )
    assert _verify(reply).status == "pass"


def test_a_gitlab_work_item_url_is_cited_as_the_store_holds_it(root):
    url = "https://gitlab.com/acme/platform/gitops/-/work_items/4"
    _write(COMPLIANCE, issue_url=url, issue_number=4, written=BEFORE, repo="gitlab.com/acme/platform/gitops")
    assert _verify(f"compliance-audit: UPDATED, ledger {url}", audits=(COMPLIANCE,)).status == "pass"
    assert _verify("compliance-audit: UPDATED.", audits=(COMPLIANCE,)).status == "fail"


def test_a_trailing_slash_or_fragment_after_the_url_still_cites_it(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    for tail in ("/", "/#issuecomment-1", ")", "."):
        assert _verify(f"compliance-audit: {URL_COMPLIANCE}{tail}", audits=(COMPLIANCE,)).status == "pass", tail


def test_a_markdown_escaped_url_or_id_is_still_read(root):
    url = "https://gitlab.com/acme/platform/gitops/-/work_items/4"
    _write(COMPLIANCE, issue_url=url, issue_number=4, written=BEFORE, repo="gitlab.com/acme/platform/gitops")
    reply = "compliance\\-audit: UPDATED, ledger https://gitlab.com/acme/platform/gitops/-/work\\_items/4"
    assert "\\_" in reply and "\\-" in reply
    assert _verify(reply, audits=(COMPLIANCE,)).status == "pass"


def test_a_longer_issue_number_does_not_cite_a_shorter_one():
    assert not cites("see https://github.com/acme/gitops/issues/120", "https://github.com/acme/gitops/issues/12")
    assert cites("see HTTPS://GitHub.com/Acme/GitOps/issues/12/", "https://github.com/acme/gitops/issues/12")


def test_an_audit_the_reply_never_mentions_fails(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    result = _verify(f"compliance-audit: {URL_COMPLIANCE}")
    assert result.status == "fail"
    assert "fleet-wide-cost-analysis: the reply does not mention it" in result.reason


def test_a_stream_that_never_ran_passes_without_a_url(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    result = _verify(f"compliance-audit: {URL_COMPLIANCE}\nfleet-wide-cost-analysis: no record in the store.")
    assert result.status == "pass", result.reason


def test_a_layout_that_names_the_audits_once_passes(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    reply = (
        "Reports for compliance-audit and fleet-wide-cost-analysis:\n"
        f"| Security | UPDATED | {URL_COMPLIANCE} |\n| Cost | no record in the store | |\n"
    )
    assert _verify(reply).status == "pass"


def test_a_clean_run_with_no_ledger_passes_without_a_url(root):
    _write(COMPLIANCE, issue_url=None, issue_number=None, written=BEFORE, status="CLEAN")
    result = _verify("compliance-audit: clean, no open ledger.", audits=(COMPLIANCE,))
    assert result.status == "pass", result.reason


def test_a_first_run_that_lands_during_the_turn_accepts_either_answer(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=DURING)
    assert _verify("compliance-audit: no record yet.", audits=(COMPLIANCE,)).status == "pass"
    assert _verify(f"compliance-audit: {URL_COMPLIANCE}", audits=(COMPLIANCE,)).status == "pass"


def test_a_clean_run_with_no_ledger_during_the_turn_accepts_either_answer(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    _write(COMPLIANCE, issue_url=None, issue_number=None, written=DURING, status="CLEAN")
    assert _verify("compliance-audit: clean now.", audits=(COMPLIANCE,)).status == "pass"
    assert _verify(f"compliance-audit: {URL_COMPLIANCE}", audits=(COMPLIANCE,)).status == "pass"


def test_a_clean_close_keeps_the_closed_ledgers_url(root):
    # audit_report's clean close stores the URL of the issue it just closed.
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=DURING, status="CLEAN")
    assert _verify(f"compliance-audit: clean, closed {URL_COMPLIANCE}", audits=(COMPLIANCE,)).status == "pass"
    assert _verify("compliance-audit: clean now.", audits=(COMPLIANCE,)).status == "fail"


def test_a_null_store_for_one_repository_does_not_clear_anothers_ledger(root):
    _write(COMPLIANCE, issue_url=None, issue_number=None, written=BEFORE, status="NO_FORGE", repo="no_forge/local")
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    result = _verify("compliance-audit: no forge.", audits=(COMPLIANCE,))
    assert result.status == "fail"
    assert URL_COMPLIANCE in result.reason


def test_a_run_stamped_before_the_turn_but_written_during_it_accepts_both_ledgers(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE_NEW, issue_number=31, written=STARTED - 600)
    directory = AUDIT_REPORT.reports_dir_for(COMPLIANCE, OWNER_REPO)
    newest = sorted((directory / "runs").iterdir())[-1]
    for path in (newest, directory / "latest.json"):
        os.utime(path, (DURING, DURING))
    assert _verify(f"compliance-audit: {URL_COMPLIANCE}", audits=(COMPLIANCE,)).status == "pass"
    assert _verify(f"compliance-audit: {URL_COMPLIANCE_NEW}", audits=(COMPLIANCE,)).status == "pass"
    assert _verify("compliance-audit: no record.", audits=(COMPLIANCE,)).status == "fail"


def test_an_older_ledger_than_the_newest_before_the_turn_fails(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE - 3600)
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE_NEW, issue_number=31, written=BEFORE)
    result = _verify(f"compliance-audit: {URL_COMPLIANCE}", audits=(COMPLIANCE,))
    assert result.status == "fail"
    assert URL_COMPLIANCE_NEW in result.reason


def test_a_missing_latest_reads_the_ring(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    (AUDIT_REPORT.reports_dir_for(COMPLIANCE, OWNER_REPO) / "latest.json").unlink()
    assert _verify(f"compliance-audit: {URL_COMPLIANCE}", audits=(COMPLIANCE,)).status == "pass"
    assert _verify("compliance-audit: 3 critical.", audits=(COMPLIANCE,)).status == "fail"


def test_an_envelope_with_no_issue_url_key_is_an_error(root):
    _write(COMPLIANCE, issue_url=URL_COMPLIANCE, issue_number=12, written=BEFORE)
    latest = AUDIT_REPORT.reports_dir_for(COMPLIANCE, OWNER_REPO) / "latest.json"
    latest.write_text(latest.read_text().replace('"issue_url"', '"ledger_url"'))
    os.utime(latest, (DURING, DURING))
    result = _verify(f"compliance-audit: {URL_COMPLIANCE}", audits=(COMPLIANCE,))
    assert result.status == "error"
    assert "no issue_url key" in result.reason


def test_an_unreadable_store_pod_is_an_error(root, monkeypatch):
    monkeypatch.setattr(onboarding, "sandbox_shell", _silent_shell)
    result = _verify(f"compliance-audit: {URL_COMPLIANCE}", audits=(COMPLIANCE,))
    assert result.status == "error"
    assert f"shell sandbox pod {onboarding.sandbox_pod()}" in result.reason


def test_an_absent_store_root_passes_on_mentions_and_the_record_says_so(root):
    result = _verify("compliance-audit: never ran.\nfleet-wide-cost-analysis: never ran.")
    assert result.status == "pass", result.reason
    assert result.raw["root_exists"] is False
    assert "nothing stored yet" in result.reason
    assert result.raw["pod"] == onboarding.sandbox_pod()


def test_a_reader_the_agent_container_does_not_give_is_an_error(root, monkeypatch):
    monkeypatch.setattr(onboarding, "agent_shell", _silent_shell)
    result = _verify(f"compliance-audit: {URL_COMPLIANCE}", audits=(COMPLIANCE,))
    assert result.status == "error"
    assert "agent container" in result.reason


def test_an_id_is_mentioned_in_any_case_and_with_any_separators():
    reply = "Fleet wide cost analysis: fine. COMPLIANCE_AUDIT: fine."
    assert mentioned_audits(reply, [COMPLIANCE, COST]) == {COMPLIANCE, COST}


def test_underscore_emphasis_around_an_id_is_still_a_mention():
    assert mentioned_audits("_compliance-audit_: UPDATED", [COMPLIANCE]) == {COMPLIANCE}


def test_an_id_inside_a_longer_name_is_not_a_mention():
    for reply in ("security-patch-orchestrator-v2 only", "old-security-patch-orchestrator only"):
        assert mentioned_audits(reply, ["security-patch-orchestrator"]) == set(), reply


def test_the_check_is_registered():
    entry_points = tomllib.loads((REPO / "bench" / "pyproject.toml").read_text())["project"]["entry-points"]
    assert entry_points["devops_bench.verifiers"]["report_store_reply"] == (
        "kube_agents_bench.report_store_reply:ReportStoreReplyVerifier"
    )


def test_the_reader_path_is_where_the_agent_image_puts_the_repository_file():
    dockerfile = (REPO / "deploy" / "docker" / "Dockerfile").read_text()
    assert "COPY --chown=hermes:hermes agents/platform/skills/ /opt/platform-template/skills/" in dockerfile
    relative = report_store_reply.READER_PATH.removeprefix("/opt/platform-template/skills/")
    assert (REPO / "agents" / "platform" / "skills" / relative) == SCRIPTS / "report_status.py"


def test_the_case_grades_every_audit_its_prompt_names():
    task = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    ids = re.search(r"from the report store: (.*)\.$", task["prompt"].strip()).group(1).split(", ")
    checks = [n["check"] for n in task["verification_spec"] if n["check"]["type"] == "report_store_reply"]
    assert len(checks) == 1
    assert checks[0]["audits"] == ids
    assert set(ids) == set(AUDIT_REPORT.AUDITS)
