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

"""The ``oobe_late_report`` verifier and the stack's late.py.

The read command runs under ``sh`` with this interpreter standing in for the agent's, against
state files, an ``executions`` table with Hermes' schema and saved run documents in a temporary
directory. late.py runs as the stack runs it, with a stand-in ``cron.jobs`` on the path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import oobe_late

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "oobe-late-first-message" / "task.yaml"
STACK = REPO / "bench" / "tf" / "prebuilt" / "oobe-late-first-message"
LATE = STACK / "late.py"
DELIVERY = REPO / "agents" / "chat" / "scripts" / "bootstrap_delivery.py"

# Hermes' cron/executions.db, as the store creates it (test_bootstrap_delivered.py).
SCHEMA = """
CREATE TABLE executions (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  source TEXT NOT NULL,
  process_id TEXT NOT NULL,
  pid INTEGER NOT NULL,
  process_started_at INTEGER,
  status TEXT NOT NULL CHECK(status IN
    ('claimed','running','completed','failed','unknown','skipped')),
  handoff_pending INTEGER NOT NULL DEFAULT 0,
  handoff_started_at REAL,
  claimed_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  skip_reason TEXT,
  delivery_outcome TEXT,
  scheduled_instant TEXT
)
"""
ARMED = datetime.now(timezone.utc) - timedelta(hours=2)


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


def _doc(directory: Path, name: str, response: str, at: datetime) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(f"# Cron Job\n\n## Prompt\n\nhttps://github.com/o/r/issues/99\n\n## Response\n\n{response}\n")
    os.utime(path, (at.timestamp(), at.timestamp()))


class Install:
    def __init__(self, root: Path) -> None:
        self.arm_state = root / ".bench-oobe.json"
        self.late_state = root / ".bench-oobe-late.json"
        self.db = root / "executions.db"
        self.out = root / "platform-output"
        self.oobe = root / "oobe-output"
        with sqlite3.connect(self.db) as con:
            con.execute(SCHEMA)
        self.rows = 0
        self.arm_state.write_text(json.dumps({"applied_at": ARMED.isoformat()}))
        self.opened = ARMED + timedelta(hours=1)
        self.late_state.write_text(json.dumps({"opened_at": self.opened.isoformat()}))

    def run(self, audit: str, claimed: datetime, outcome: str | None, ledger: str | None) -> None:
        self.rows += 1
        finished = claimed + timedelta(minutes=5)
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, finished_at, delivery_outcome)"
                " VALUES (?,?,?,?,?,'completed',?,?,?)",
                (f"{self.rows:032x}", audit, "builtin", "p", 1, claimed.isoformat(), finished.isoformat(), outcome),
            )
        body = f"Let's format it.\n- **Ledger Issue:** [Audit]({ledger})" if ledger else "No ledger this time."
        _doc(self.out / audit, str(self.rows), body, finished)

    def chain(self, outcome: str | None = "failed") -> None:
        for i, audit in enumerate(oobe_late.FIRST_RUN_AUDITS):
            self.run(audit, ARMED + timedelta(minutes=10 * i), outcome, f"https://github.com/o/r/issues/{i + 1}")

    def deliver(self, note: str, at: datetime | None = None) -> None:
        _doc(self.oobe, "report", f"# {oobe_late.REPORT_TITLE}\n\nStand-in.\n\n{note}", at or self.opened + timedelta(minutes=1))


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    s = Install(tmp_path)
    monkeypatch.setattr(oobe_late, "STATE_FILE", str(s.arm_state))
    monkeypatch.setattr(oobe_late, "LATE_STATE_FILE", str(s.late_state))
    monkeypatch.setattr(oobe_late, "PLATFORM_EXECUTIONS_DB", str(s.db))
    monkeypatch.setattr(oobe_late, "PLATFORM_OUTPUT_DIR", str(s.out))
    monkeypatch.setattr(oobe_late, "OOBE_OUTPUT_DIR", str(s.oobe))
    monkeypatch.setattr(oobe_late, "HERMES_PYTHON", sys.executable)
    monkeypatch.setattr(oobe_late, "agent_shell", _local_shell)
    return s


def _verify():
    return oobe_late.OobeLateReportVerifier(type="oobe_late_report").verify(0.0)


# --- the names ------------------------------------------------------------


def test_the_case_names_this_check_and_the_entry_point_is_registered() -> None:
    spec = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["verification_spec"]
    assert any(e["check"].get("type") == "oobe_late_report" for e in spec)
    points = tomllib.loads((REPO / "bench" / "pyproject.toml").read_text())["project"]["entry-points"]
    assert points["devops_bench.verifiers"]["oobe_late_report"] == "kube_agents_bench.oobe_late:OobeLateReportVerifier"


def test_the_names_match_the_stack_and_the_delivery() -> None:
    assert '".bench-oobe-late.json"' in LATE.read_text()
    assert (STACK / "report.txt").read_text().startswith(f"# {oobe_late.REPORT_TITLE}\n")
    assert oobe_late.NOT_POSTED in DELIVERY.read_text()


# --- the verdict ----------------------------------------------------------


def test_a_report_that_links_every_failed_summary_passes(install: Install) -> None:
    install.chain()
    install.deliver("These finished ... " + ", ".join(f"https://github.com/o/r/issues/{i}" for i in range(1, 5)))
    result = _verify()
    assert result.status == "pass", result.reason


def test_the_note_main_sends_fails_and_names_the_missing_links(install: Install) -> None:
    install.chain()
    install.deliver("Workload checks come from the first-run audits, which have started: `compliance-audit`.")
    result = _verify()
    assert result.status == "fail"
    assert "compliance-audit (https://github.com/o/r/issues/2)" in result.reason


def test_an_audit_with_no_link_must_be_named_as_not_posted(install: Install) -> None:
    install.run("compliance-audit", ARMED, "failed", None)
    install.deliver("which have started: `compliance-audit`.")
    assert _verify().status == "fail"
    install.deliver(f"so their {oobe_late.NOT_POSTED}: `compliance-audit`.", at=install.opened + timedelta(minutes=2))
    result = _verify()
    assert result.status == "pass", result.reason


def test_delivered_summaries_need_no_mention(install: Install) -> None:
    install.chain()
    install.run("compliance-audit", ARMED + timedelta(minutes=50), "delivered", "https://github.com/o/r/issues/7")
    install.deliver("https://github.com/o/r/issues/1 https://github.com/o/r/issues/3 https://github.com/o/r/issues/4")
    result = _verify()
    assert result.status == "pass", result.reason


def test_no_failed_summary_is_an_error(install: Install) -> None:
    install.chain(outcome="delivered")
    install.deliver("anything")
    result = _verify()
    assert result.status == "error"
    assert "home channel was not taken away" in result.reason


def test_a_run_from_before_the_arm_does_not_count(install: Install) -> None:
    install.run("compliance-audit", ARMED - timedelta(days=1), "failed", "https://github.com/o/r/issues/2")
    install.deliver("nothing")
    assert _verify().status == "error"


def test_no_delivered_report_fails(install: Install) -> None:
    install.chain()
    # A saved oobe run from before the chat was opened is not the delivery.
    install.deliver("https://github.com/o/r/issues/1", at=install.opened - timedelta(minutes=1))
    result = _verify()
    assert result.status == "fail"
    assert "nothing was delivered" in result.reason


def test_a_link_on_the_ledger_line_wins(install: Install) -> None:
    install.run("compliance-audit", ARMED, "failed", None)
    _doc(install.out / "compliance-audit", "z", "See https://github.com/k/k/issues/5\nLedger: https://github.com/o/r/issues/3",
         ARMED + timedelta(minutes=6))
    install.deliver("https://github.com/k/k/issues/5")
    assert _verify().status == "fail"


def test_the_prompt_links_in_a_saved_run_are_not_its_ledger(install: Install) -> None:
    install.run("compliance-audit", ARMED, "failed", None)
    install.deliver("https://github.com/o/r/issues/99")
    assert _verify().status == "fail"


def test_an_unarmed_stack_is_an_error(install: Install) -> None:
    install.late_state.write_text(json.dumps({"opened_at": None}))
    assert _verify().status == "error"


def test_a_pod_that_does_not_answer_is_an_error(install: Install, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oobe_late, "agent_shell", lambda script, timeout: "")
    assert _verify().status == "error"


# --- late.py --------------------------------------------------------------

CONFIG = """\
model:
  default: gemini
platforms:
  google_chat:
    enabled: true
    home_channel:
      platform: google_chat
      chat_id: spaces/AAA
      name: DM
"""
ENV = "GEMINI_API_KEY=x\nGOOGLE_CHAT_HOME_CHANNEL=spaces/AAA\nGOOGLE_CHAT_HOME_CHANNEL_THREAD_ID=\n"


def _late(home: Path, *args: str, jobs: list | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    stub = home.parent / "stub"
    (stub / "cron").mkdir(parents=True, exist_ok=True)
    (stub / "cron" / "__init__.py").write_text("")
    (stub / "cron" / "jobs.py").write_text(f"def load_jobs():\n    return {jobs or []!r}\n")
    clean = {k: v for k, v in os.environ.items() if not k.endswith("_HOME_CHANNEL")}
    return subprocess.run(
        [sys.executable, "-", str(home), *args], input=LATE.read_text(), capture_output=True, text=True,
        env={**clean, "PYTHONPATH": str(stub), **(env or {})},
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "data"
    h.mkdir()
    (h / "config.yaml").write_text(CONFIG)
    (h / ".env").write_text(ENV)
    (h / ".user_aligned").write_text("")
    (h / ".bootstrap_completed").write_text("1760000000\n")
    return h


def test_clear_takes_the_home_channel_away_and_restore_puts_it_back_byte_for_byte(home: Path) -> None:
    assert _late(home, "clear").returncode == 0
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert "home_channel" not in cfg["platforms"]["google_chat"]
    assert cfg["platforms"]["google_chat"]["enabled"] is True
    assert (home / ".env").read_text() == "GEMINI_API_KEY=x\n"
    assert not (home / ".user_aligned").exists() and not (home / ".bootstrap_completed").exists()
    assert _late(home, "open").returncode == 0
    assert (home / ".user_aligned").exists()
    assert _late(home, "restore").returncode == 0
    assert (home / "config.yaml").read_text() == CONFIG
    assert (home / ".env").read_text() == ENV
    assert (home / ".bootstrap_completed").read_text() == "1760000000\n"
    assert not (home / ".bench-oobe-late.json").exists()


def test_clear_refuses_a_home_channel_in_the_container_environment(home: Path) -> None:
    res = _late(home, "clear", env={"GOOGLE_CHAT_HOME_CHANNEL": "spaces/CR"})
    assert res.returncode != 0 and "CR's homeChannel" in res.stderr
    assert (home / "config.yaml").read_text() == CONFIG


def test_clear_refuses_an_oobe_job_bound_to_a_chat(home: Path) -> None:
    res = _late(home, "clear", jobs=[{"id": "oobe", "deliver": "origin"}])
    assert res.returncode != 0 and "would be posted there" in res.stderr
    assert not (home / ".bench-oobe-late.json").exists()


def test_finished_waits_for_every_audit_since_the_arm(home: Path) -> None:
    (home / ".bench-oobe.json").write_text(json.dumps({"applied_at": ARMED.isoformat()}))
    stage = home.parent / "oobe.py"
    stage.write_text(f"FIRST_RUN_AUDITS = {oobe_late.FIRST_RUN_AUDITS!r}\n")
    db = home / "profiles" / "platform" / "cron" / "executions.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as con:
        con.execute(SCHEMA)
        for i, audit in enumerate(oobe_late.FIRST_RUN_AUDITS):
            status = "running" if i == 3 else "completed"
            con.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at) VALUES (?,?,?,?,?,?,?)",
                (str(i), audit, "builtin", "p", 1, status, (ARMED + timedelta(minutes=i)).isoformat()),
            )
    env = {"OOBE_STAGE_SOURCE": str(stage)}
    assert _late(home, "finished", env=env).stdout.strip() == "pending"
    # A skipped row ran nothing.
    with sqlite3.connect(db) as con:
        con.execute("UPDATE executions SET status = 'skipped' WHERE id = '3'")
    assert _late(home, "finished", env=env).stdout.strip() == "pending"
    with sqlite3.connect(db) as con:
        con.execute("UPDATE executions SET status = 'completed' WHERE id = '3'")
    assert _late(home, "finished", env=env).stdout.strip() == "finished"
    # An audit the deployed stage adds is waited for too.
    stage.write_text(f"FIRST_RUN_AUDITS = {oobe_late.FIRST_RUN_AUDITS + ('upgrade-retrospective',)!r}\n")
    assert _late(home, "finished", env=env).stdout.strip() == "pending"


def test_the_stage_audit_list_late_reads_is_the_stage_s() -> None:
    import ast
    tree = ast.parse((REPO / "agents" / "chat" / "scripts" / "oobe.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "FIRST_RUN_AUDITS")
    assert tuple(ast.literal_eval(node.value)) == oobe_late.FIRST_RUN_AUDITS
