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

"""The ``audit_chat_report_posted`` verdict, and the reads behind it.

The gateway read runs under ``sh`` with this interpreter standing in for the
agent's, against a state file, a Session KV store and a cron output directory in
a temporary directory.
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from kube_agents_bench import audit_chat_report as acr

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "fleet-audit-chat-report-is-the-report" / "task.yaml"
FIRED = datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc)
REPORT = "Security & RBAC Posture Audit: 0 new, 3 resolved; 2 findings (0 critical) across 4 cluster(s).\nhttps://github.com/acme/fleet/issues/3"
NOTES = "silent_ok is false (resolved: 3).\nLet's format the final response!## Security & RBAC Posture Audit"


def gateway(posted, ended=True, response="report", outcome="delivered", threads=1):
    state = {"job_id": "compliance-audit", "fired_at": FIRED.isoformat()}
    if ended:
        state.update(last_run_at=(FIRED + timedelta(minutes=20)).isoformat(), delivery_outcome=outcome)
    return {"state": state, "posted": posted, "threads": threads, "response": response}


def record(parts, started=FIRED + timedelta(minutes=1)):
    return {"run": "r1", "started_at": started.timestamp(), "parts": [{"repo": "acme/fleet", "text": t} for t in parts]}


def test_the_report_finish_rendered_passes():
    status, _ = acr.verdict(gateway([REPORT]), record([REPORT]))
    assert status == "pass"


def test_every_repositorys_part_is_one_post():
    posted = "a\n\nb\n\nSecurity & RBAC Posture Audit: 2 repositories reported in this run."
    status, _ = acr.verdict(gateway([posted]), record(["a", "b"]))
    assert status == "pass"


def test_a_closing_line_with_the_wrong_count_fails():
    posted = "a\n\nb\n\nSecurity & RBAC Posture Audit: 3 repositories reported in this run."
    status, _ = acr.verdict(gateway([posted]), record(["a", "b"]))
    assert status == "fail"


def test_a_repository_that_did_not_finish_fails():
    rec = record([REPORT])
    rec["parts"].append({"repo": "acme/other", "text": None})
    status, reason = acr.verdict(gateway([REPORT]), rec)
    assert status == "fail"
    assert "acme/other" in reason


def test_the_final_message_with_notes_fails():
    status, reason = acr.verdict(gateway([NOTES]), record([REPORT]))
    assert status == "fail"
    assert "not the report finish rendered" in reason


def test_no_report_from_finish_is_the_red_on_an_image_that_posts_the_final_message():
    status, reason = acr.verdict(gateway([NOTES]), None)
    assert status == "fail"
    assert "left no report from finish" in reason


def test_an_earlier_runs_report_does_not_count():
    status, _ = acr.verdict(gateway([REPORT]), record([REPORT], started=FIRED - timedelta(hours=1)))
    assert status == "fail"


def test_a_non_finite_start_stamp_does_not_pass_the_fire_check():
    rec = record([REPORT])
    rec["started_at"] = float("inf")
    status, reason = acr.verdict(gateway([REPORT]), rec)
    assert status == "fail"
    assert "left no report from finish" in reason


def test_a_silent_run_is_an_error():
    status, _ = acr.verdict(gateway([], response="**[SILENT]**\n"), record([]))
    assert status == "error"


def test_a_run_that_has_not_ended_is_an_error():
    status, _ = acr.verdict(gateway([], ended=False), None)
    assert status == "error"


def test_a_failed_delivery_fails_with_its_error():
    status, reason = acr.verdict(gateway([], outcome="failed: chat relay answered HTTP 502"), record([REPORT]))
    assert status == "fail"
    assert "HTTP 502" in reason


def test_an_oversized_record_fails_naming_the_size():
    big = acr.SANDBOX_READ + "x" * (acr.MAX_REPORT_BYTES + 1)
    read, rec = acr.read_chat_report(lambda c, t: big, "compliance-audit", 1)
    assert read
    status, reason = acr.verdict(gateway([REPORT]), rec)
    assert status == "fail"
    assert "over" in reason and str(acr.MAX_REPORT_BYTES) in reason


def test_no_thread_is_an_error():
    status, _ = acr.verdict(gateway([], threads=0), record([REPORT]))
    assert status == "error"


def test_the_gateway_read_returns_the_rows_and_the_response(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"job_id": "compliance-audit", "fired_at": FIRED.isoformat()}))
    db = tmp_path / "kv.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE session_metadata (session_id TEXT, metadata TEXT)")
        conn.execute("CREATE TABLE incidents (chat_id TEXT, thread_id TEXT, report TEXT, created_at TIMESTAMP)")
        conn.execute(
            "INSERT INTO session_metadata VALUES (?, ?)",
            ("cron-platform-compliance-audit-2026-10-15",
             json.dumps({"platform_threads": {"google_chat": {"chat_id": "spaces/S", "thread_id": "T"}}})),
        )
        conn.execute("INSERT INTO incidents VALUES ('spaces/S', 'T', 'old', '2026-10-15 17:00:00')")
        conn.execute("INSERT INTO incidents VALUES ('spaces/S', 'T', ?, '2026-10-15 18:20:00')", (REPORT,))
    output = tmp_path / "output" / "compliance-audit"
    output.mkdir(parents=True)
    run = output / "2026-10-15_18-20-00.md"
    run.write_text("# Cron Job\n\n## Prompt\n\nx\n\n## Response\n\n" + REPORT)
    os.utime(run, (FIRED.timestamp() + 60, FIRED.timestamp() + 60))
    script = acr._GATEWAY_SCRIPT
    args = [str(state), str(db), "platform", str(tmp_path / "output"), acr.GATEWAY_READ]
    out = subprocess.run(
        ["sh", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} " + " ".join(shlex.quote(a) for a in args)],
        capture_output=True, text=True, check=True, env={**os.environ, "SESSION_KV_DB_PATH": ""},
    ).stdout
    read = acr.read_gateway(lambda command, timeout: out, 1)
    assert read["posted"] == [REPORT]
    assert read["response"].strip() == REPORT


def test_the_sandbox_read_tells_absent_from_unreadable():
    assert acr.read_chat_report(lambda c, t: acr.SANDBOX_ABSENT, "compliance-audit", 1) == (True, None)
    assert acr.read_chat_report(lambda c, t: "", "compliance-audit", 1) == (False, None)
    body = json.dumps(record([REPORT]))
    assert acr.read_chat_report(lambda c, t: acr.SANDBOX_READ + body, "compliance-audit", 1) == (True, json.loads(body))


def test_the_check_is_registered_and_the_task_names_it():
    entry_points = tomllib.loads((REPO / "bench" / "pyproject.toml").read_text())["project"]["entry-points"]
    assert entry_points["devops_bench.verifiers"]["audit_chat_report_posted"].endswith(":AuditChatReportPostedVerifier")
    task = yaml.safe_load(TASK.read_text())
    assert task["verification_spec"][0]["check"]["type"] == "audit_chat_report_posted"


def _arm():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "audit_chat_report_arm", REPO / "bench" / "tf" / "prebuilt" / "audit-chat-report" / "arm.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _home(tmp_path, rows):
    cron = tmp_path / "profiles" / "platform" / "cron"
    cron.mkdir(parents=True)
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{"id": "compliance-audit"}]}))
    with sqlite3.connect(cron / "executions.db") as conn:
        conn.execute(
            "CREATE TABLE executions (job_id TEXT, status TEXT, claimed_at TEXT, started_at TEXT, finished_at TEXT, delivery_outcome TEXT)"
        )
        conn.executemany(
            "INSERT INTO executions VALUES ('compliance-audit', ?, ?, ?, ?, ?)",
            [(status, claimed, None if status == "skipped" or (status == "failed" and outcome == "never-started") else claimed, finished, outcome)
             for status, claimed, finished, outcome in rows],
        )
    return str(tmp_path)


def test_wait_reads_only_a_run_claimed_after_the_mark(tmp_path):
    arm = _arm()
    before = (FIRED - timedelta(minutes=5)).isoformat()
    after = (FIRED + timedelta(minutes=1)).isoformat()
    done = (FIRED + timedelta(minutes=20)).isoformat()
    home = _home(tmp_path, [
        ("completed", before, (FIRED + timedelta(seconds=30)).isoformat(), "delivered"),
        ("completed", after, done, "delivered"),
    ])
    Path(home, arm.STATE_NAME).write_text(json.dumps({"job_id": "compliance-audit", "fired_at": FIRED.isoformat()}))
    arm.wait(home, "compliance-audit", 1)
    state = json.loads(Path(home, arm.STATE_NAME).read_text())
    assert state["last_run_at"] == done
    assert state["delivery_outcome"] == "delivered"


def test_wait_does_not_take_an_earlier_runs_end(tmp_path):
    arm = _arm()
    home = _home(tmp_path, [
        ("completed", (FIRED - timedelta(minutes=5)).isoformat(), (FIRED + timedelta(seconds=30)).isoformat(), "delivered"),
    ])
    Path(home, arm.STATE_NAME).write_text(json.dumps({"job_id": "compliance-audit", "fired_at": FIRED.isoformat()}))
    arm.POLL_S = 0
    try:
        arm.wait(home, "compliance-audit", 0)
    except SystemExit as exc:
        assert "did not finish" in str(exc)
    else:
        raise AssertionError("wait accepted a run claimed before the mark")


def test_idle_waits_out_a_running_run(tmp_path):
    arm = _arm()
    home = _home(tmp_path, [("running", FIRED.isoformat(), None, None)])
    arm.POLL_S = 0
    try:
        arm.idle(home, "compliance-audit", 0)
    except SystemExit as exc:
        assert "still claimed or running" in str(exc)
    else:
        raise AssertionError("idle returned while a run was going")
    idle_home = _home(tmp_path / "idle", [("completed", FIRED.isoformat(), FIRED.isoformat(), "delivered")])
    arm.idle(idle_home, "compliance-audit", 0)


def test_wait_ignores_a_skipped_occurrence(tmp_path):
    arm = _arm()
    home = _home(tmp_path, [
        ("skipped", (FIRED + timedelta(seconds=5)).isoformat(), (FIRED + timedelta(seconds=5)).isoformat(), None),
    ])
    Path(home, arm.STATE_NAME).write_text(json.dumps({"job_id": "compliance-audit", "fired_at": FIRED.isoformat()}))
    arm.POLL_S = 0
    try:
        arm.wait(home, "compliance-audit", 0)
    except SystemExit as exc:
        assert "did not finish" in str(exc)
    else:
        raise AssertionError("wait took a skipped row as the fired run")


def test_wait_ignores_an_occurrence_that_never_started(tmp_path):
    arm = _arm()
    home = _home(tmp_path, [
        ("failed", (FIRED + timedelta(seconds=5)).isoformat(), (FIRED + timedelta(seconds=6)).isoformat(), "never-started"),
    ])
    Path(home, arm.STATE_NAME).write_text(json.dumps({"job_id": "compliance-audit", "fired_at": FIRED.isoformat()}))
    arm.POLL_S = 0
    try:
        arm.wait(home, "compliance-audit", 0)
    except SystemExit as exc:
        assert "did not finish" in str(exc)
    else:
        raise AssertionError("wait took a never-started row as the fired run")


def test_fire_refuses_a_disabled_job(tmp_path):
    arm = _arm()
    cron = tmp_path / "profiles" / "platform" / "cron"
    cron.mkdir(parents=True)
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{"id": "compliance-audit", "enabled": False}]}))
    try:
        arm.fire(str(tmp_path), "python3", "compliance-audit")
    except SystemExit as exc:
        assert "paused or disabled" in str(exc)
    else:
        raise AssertionError("fire triggered a disabled job")
    assert not (tmp_path / arm.STATE_NAME).exists()
