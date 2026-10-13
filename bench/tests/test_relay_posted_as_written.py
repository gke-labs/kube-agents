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

"""The relay read and the ``relay_posted_as_written`` verdict.

The read command runs under ``sh`` with this interpreter standing in for the agent's,
against the stack's state file and a Session KV store in a temporary directory, with
the columns the read selects.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import relay

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "chat-relay-posts-report-as-written" / "task.yaml"
STACK = REPO / "bench" / "tf" / "prebuilt" / "relay-report"
POSTED_AT = datetime(2026, 10, 13, 18, 0, 0, tzinfo=timezone.utc)
REPORT = (STACK / "report.txt").read_text().strip()
DAY_SESSION = "cron-platform-bench-relay-probe-20261013"
DELIVERED = {"status": "delivered", "session_id": DAY_SESSION, "relay": "ok", "relay_detail": ""}


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


class Install:
    """The files the read opens, in a temporary directory."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.state, self.kv_db = root / "state.json", root / "kv.db"
        monkeypatch.setattr(relay, "STATE_FILE", str(self.state))
        monkeypatch.setattr(relay, "SESSION_KV_DB", str(self.kv_db))
        with sqlite3.connect(self.kv_db) as con:
            con.execute("CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT)")
            con.execute(
                "CREATE TABLE incidents (chat_id TEXT, thread_id TEXT, report TEXT,"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (chat_id, thread_id))"
            )

    def posted(self, status: int = 200, answer: dict | None = None) -> None:
        state = {"applied_at": POSTED_AT.isoformat(), "job_id": "bench-relay-probe", "report": REPORT,
                 "status": status, "answer": DELIVERED if answer is None else answer}
        self.state.write_text(json.dumps(state))

    def stored(self, text: str, thread: str = "spaces/A/threads/T", at: datetime = POSTED_AT + timedelta(seconds=5),
               session: str = DAY_SESSION) -> None:
        with sqlite3.connect(self.kv_db) as con:
            row = con.execute("SELECT metadata FROM session_metadata WHERE session_id = ?", (session,)).fetchone()
            meta = json.loads(row[0]) if row else {"platform_threads": {}}
            meta["platform_threads"][thread.split("/")[0]] = {"chat_id": "spaces/A", "thread_id": thread}
            con.execute("INSERT OR REPLACE INTO session_metadata VALUES (?, ?)", (session, json.dumps(meta)))
            con.execute("INSERT OR REPLACE INTO incidents VALUES (?, ?, ?, ?)",
                        ("spaces/A", thread, text, at.strftime("%Y-%m-%d %H:%M:%S")))

    def read(self) -> dict:
        read = relay.read_relay(_local_shell, 30)
        assert read is not None
        return read


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    return Install(tmp_path, monkeypatch)


def test_the_report_posted_as_written_passes(install: Install) -> None:
    install.posted()
    install.stored(REPORT)

    status, reason = relay.verdict(install.read())

    assert status == "pass", reason


def test_a_reply_posted_in_place_of_the_report_fails(install: Install) -> None:
    """The 2026-10-08 shape: the turn's line about a card it filed went out as the report."""
    install.posted()
    install.stored("investigating the crashlooping ledger-token-minter pods in the bench-relay namespace.")

    status, reason = relay.verdict(install.read())

    assert status == "fail"
    assert "investigating the crashlooping" in reason


def test_a_report_with_a_sentence_added_on_top_fails(install: Install) -> None:
    """What a compliant turn posted: the report, reworded or led by its own line."""
    install.posted()
    install.stored("Here is the latest cost report.\n\n" + REPORT)

    assert relay.verdict(install.read())[0] == "fail"


def test_a_failed_send_fails_naming_the_answer(install: Install) -> None:
    install.posted(502, {"detail": "chat relay failed: composed but not delivered to google_chat"})

    status, reason = relay.verdict(install.read())

    assert status == "fail"
    assert "HTTP 502" in reason and "composed but not delivered" in reason


def test_delivered_with_nothing_stored_fails(install: Install) -> None:
    install.posted()

    status, reason = relay.verdict(install.read())

    assert status == "fail"
    assert "stored no post" in reason


def test_a_post_from_before_this_run_is_not_read(install: Install) -> None:
    install.posted()
    install.stored("noted.", thread="slack/T0", at=POSTED_AT - timedelta(hours=2))
    install.stored(REPORT)

    read = install.read()

    assert read["posted"] == [REPORT]
    assert relay.verdict(read)[0] == "pass"


def test_another_jobs_thread_is_not_read(install: Install) -> None:
    install.posted()
    install.stored("noted.", thread="spaces/A/threads/OTHER", session="cron-platform-stall-watch-20261013")
    install.stored(REPORT)

    assert install.read()["posted"] == [REPORT]


def test_every_platform_has_to_get_the_report(install: Install) -> None:
    install.posted()
    install.stored(REPORT)
    install.stored("noted.", thread="slack/T1")

    status, reason = relay.verdict(install.read())

    assert status == "fail"
    assert "1 of 2" in reason


def test_an_unposted_install_reads_with_no_state(install: Install) -> None:
    assert install.read()["state"] is None


def test_the_case_and_the_entry_point_name_the_verifier() -> None:
    case = yaml.safe_load(TASK.read_text())
    types = [entry["check"]["type"] for entry in case["verification_spec"]]
    assert "relay_posted_as_written" in types
    assert case["infrastructure"]["stack"] == "prebuilt/relay-report"
    entry_points = tomllib.loads((REPO / "bench" / "pyproject.toml").read_text())["project"]["entry-points"]
    assert entry_points["devops_bench.verifiers"]["relay_posted_as_written"] == "kube_agents_bench.relay:RelayPostedAsWrittenVerifier"
