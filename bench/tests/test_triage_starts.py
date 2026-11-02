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

"""The ``triage_starts`` verifier: its in-pod read against local SQLite files, and its grading.

The read command runs here through a local shell against a temporary Session KV store and a
temporary Hermes ``state.db`` with the tables the pod's stores carry, so the SQL and the
join are what the pod runs. ``verdict`` is pure and is driven directly.
"""

from __future__ import annotations

import sqlite3
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import triage_starts
from kube_agents_bench.triage_starts import TriageStartsVerifier, read_starts, verdict

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "triage-start-burst" / "task.yaml"
NAMESPACE = "eval-triage-burst"


def _local_shell(command: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=timeout).stdout


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    kv, state = tmp_path / "session_kv.db", tmp_path / "state.db"
    with sqlite3.connect(kv) as conn:
        conn.execute(
            "CREATE TABLE intercepted_events (id INTEGER PRIMARY KEY, namespace TEXT, workload TEXT, "
            "object_uid TEXT, reason TEXT, severity TEXT, notified INTEGER, delivery_error TEXT DEFAULT '', "
            "session_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    with sqlite3.connect(state) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT)")
    monkeypatch.setattr(triage_starts, "SESSION_KV_DB", str(kv))
    monkeypatch.setattr(triage_starts, "GATEWAY_STATE_DB", str(state))
    monkeypatch.delenv("SESSION_KV_DB_PATH", raising=False)
    return kv, state


def _plant(kv: Path, state: Path, n: int, *, session: int, turn: int, notified: int = 1) -> dict[str, str]:
    """n admitted rows; the first `session` have a Hermes session, the first `turn` a message."""
    pods = {}
    with sqlite3.connect(kv) as k, sqlite3.connect(state) as s:
        for i in range(n):
            uid, sid = f"uid-{i}", f"k8s-evt-{i}"
            pods[uid] = f"pod-{i}"
            k.execute(
                "INSERT INTO intercepted_events (namespace, workload, object_uid, reason, severity, notified, "
                "session_id) VALUES (?, ?, ?, 'Failed', 'Warning', ?, ?)",
                (NAMESPACE, f"pod-{i}", uid, notified, sid),
            )
            if i < session:
                s.execute("INSERT INTO sessions (id) VALUES (?)", (sid,))
            if i < turn:
                s.execute("INSERT INTO messages (session_id, role) VALUES (?, 'user')", (sid,))
    return pods


def _grade(pods: dict[str, str], min_alerts: int = 4) -> tuple[str, str, dict[str, Any]]:
    read = read_starts(_local_shell, NAMESPACE, sorted(pods), 30)
    assert read is not None and read["error"] is None, read
    return verdict(pods, read, NAMESPACE, min_alerts)


def test_every_started_turn_passes(stores: tuple[Path, Path]) -> None:
    status, reason, raw = _grade(_plant(*stores, 4, session=4, turn=4))
    assert status == "pass", reason
    assert raw["sessions_with_messages"] == 4


def test_a_session_with_no_turn_is_a_lost_start(stores: tuple[Path, Path]) -> None:
    """A create the daemon gave up on still commits in Hermes; only the turn shows a start."""
    status, reason, raw = _grade(_plant(*stores, 4, session=4, turn=2))
    assert status == "fail"
    assert "2 of 4" in reason and "0 with no Hermes session, 2 with a session but no message" in reason
    assert raw["lost"] == ["k8s-evt-2", "k8s-evt-3"]


def test_a_missing_session_is_a_lost_start(stores: tuple[Path, Path]) -> None:
    status, reason, _ = _grade(_plant(*stores, 4, session=3, turn=3))
    assert status == "fail"
    assert "1 with no Hermes session" in reason


def test_fewer_pods_than_min_alerts_is_an_error(stores: tuple[Path, Path]) -> None:
    status, reason, _ = _grade(_plant(*stores, 3, session=3, turn=3))
    assert status == "error"
    assert "fewer than min_alerts" in reason


def test_refused_alerts_are_an_error_naming_the_limit(stores: tuple[Path, Path]) -> None:
    status, reason, _ = _grade(_plant(*stores, 4, session=0, turn=0, notified=0))
    assert status == "error"
    assert "ALERT_DAILY_LIMIT_WARNING" in reason


def test_an_undelivered_alert_still_counts_as_admitted() -> None:
    assert triage_starts.admitted({"notified": 0, "delivery_error": "no message id"})
    assert not triage_starts.admitted({"notified": 0, "delivery_error": ""})


def test_a_missing_store_is_reported_not_raised(
    stores: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(triage_starts, "GATEWAY_STATE_DB", str(tmp_path / "absent.db"))
    read = read_starts(_local_shell, NAMESPACE, ["uid-0"], 30)
    assert read is not None and "no such store" in read["error"]


def test_a_store_without_a_messages_table_is_reported_not_graded(stores: tuple[Path, Path]) -> None:
    """A schema change in a Hermes pin is an error, not twenty lost starts."""
    _, state = stores
    with sqlite3.connect(state) as conn:
        conn.execute("DROP TABLE messages")
    read = read_starts(_local_shell, NAMESPACE, ["uid-0"], 30)
    assert read is not None and "no messages table" in read["error"]


def test_an_unreadable_pod_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage_starts, "read_planted_pods", lambda ns, t: {f"uid-{i}": "p" for i in range(20)})
    monkeypatch.setattr(triage_starts, "agent_shell", lambda c, t: "")
    status, reason, _ = TriageStartsVerifier(type="triage_starts", namespace=NAMESPACE, min_alerts=20)._check(5)
    assert status == "error" and "could not be read" in reason


# --- registration ---------------------------------------------------------


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["triage_starts"] == "kube_agents_bench.triage_starts:TriageStartsVerifier"


def test_parse_node_builds_the_case_check() -> None:
    spec = yaml.safe_load(TASK.read_text())["verification_spec"]
    check = next(e["check"] for e in spec if e["check"]["type"] == "triage_starts")
    assert isinstance(parse_node(check), TriageStartsVerifier)
    assert VERIFIERS.get("triage_starts") is TriageStartsVerifier


@pytest.mark.parametrize(
    "check",
    [
        {"type": "triage_starts", "namespace": "", "min_alerts": 20},
        {"type": "triage_starts", "namespace": NAMESPACE, "min_alerts": 0},
    ],
)
def test_a_malformed_check_is_rejected_at_load(check: dict[str, Any]) -> None:
    with pytest.raises(Exception):
        parse_node(check)
