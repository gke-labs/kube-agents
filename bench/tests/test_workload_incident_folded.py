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

"""The event-ledger read and the ``workload_incident_folded`` verifier.

The read command runs here, locally, against an HTTP server that serves
``GET /v1/intercepted-events`` with the Session KV key check, the way
``test_findings_item.py`` drives the findings read.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import tomllib
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import incidents, onboarding, transcript
from kube_agents_bench.incidents import WorkloadIncidentFoldedVerifier

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "agents" / "platform" / "scripts" / "session_kv_server.py"
TASK = REPO / "bench" / "tasks" / "incident-triage-workload-fold" / "task.yaml"
API_KEY = "k-test"
NS = "eval-workload-fold"
NAME = "eval-fold-workload"


def _at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _row(row_id: int, status: str, minutes_ago: float = 2, duplicate_of: int | None = None, **overrides) -> dict:
    row = {
        "id": row_id,
        "session_id": f"k8s-evt-{row_id}",
        "timestamp": _at(minutes_ago),
        "cluster": "c1",
        "namespace": NS,
        "kind": "Pod",
        "name": NAME,
        "reason": "BackOff",
        "severity": "Warning",
        "status": status,
        "message": "Back-off restarting failed container",
        "duplicate_of": duplicate_of,
    }
    row.update(overrides)
    return row


class _Feed:
    def __init__(self) -> None:
        self.rows: list[dict] = []


def _handler(feed: _Feed):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                body, code = {"detail": "unauthorized"}, 401
            elif not self.path.startswith("/v1/intercepted-events"):
                body, code = {"detail": "no such route"}, 404
            else:
                body, code = {"events": sorted(feed.rows, key=lambda r: r["id"], reverse=True)}, 200
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Feed]:
    served = _Feed()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(served))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("SESSION_KV_API_KEY", API_KEY)
    monkeypatch.setattr(incidents, "SESSION_KV_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(onboarding, "agent_shell", _local_shell)
    started = (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()
    monkeypatch.setattr(transcript, "get", lambda: SimpleNamespace(started_at=started))
    try:
        yield served
    finally:
        server.shutdown()
        server.server_close()


def _spec_entries() -> dict[str, dict[str, Any]]:
    spec = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["verification_spec"]
    return {entry["name"]: entry for entry in spec}


def _verify(check: dict[str, Any] | None = None):
    check = check or {"type": "workload_incident_folded", "namespace": NS, "name": NAME, "min_folded": 2}
    return parse_node(check).verify(0.0)


# --- the verdicts ----------------------------------------------------------


HELD = {"type": "workload_incident_folded", "namespace": NS, "name": NAME, "min_folded": 1,
        "requires_pull_request": True, "max_age_sec": 5400}
FIX = "https://github.com/acme/fleet/pull/7"


def test_an_event_held_for_an_open_fix_passes(feed) -> None:
    feed.rows = [_row(10, "injected", minutes_ago=40), _row(11, "folded", duplicate_of=10, pull_request=FIX)]
    result = _verify(HELD)
    assert result.status == "pass", result.reason


def test_a_fold_without_a_pull_request_does_not_count_when_one_is_required(feed) -> None:
    # Folded inside the window, not held for a fix: the case grades the hold.
    feed.rows = [_row(10, "injected", minutes_ago=40), _row(11, "folded", duplicate_of=10, pull_request="")]
    result = _verify(HELD)
    assert result.status == "fail"
    assert "held for an open pull request" in result.reason


def test_a_recurrence_admitted_as_a_new_incident_fails_the_hold(feed) -> None:
    # A daemon that does not hold for an open fix: the replaced pod is a new incident.
    feed.rows = [_row(10, "injected", minutes_ago=40), _row(11, "injected")]
    assert _verify(HELD).status == "fail"


def test_a_burst_folded_into_one_incident_passes(feed) -> None:
    feed.rows = [_row(10, "injected"), _row(11, "folded", duplicate_of=10), _row(12, "folded", duplicate_of=10)]
    result = _verify()
    assert result.status == "pass", result.reason
    assert "has 2 event(s) folded into it" in result.reason


def test_a_burst_that_opened_one_incident_per_replica_fails(feed) -> None:
    # A daemon that does not fold: every replica's row is admitted, and the feed
    # carries no duplicate_of at all.
    feed.rows = [{k: v for k, v in _row(i, "injected").items() if k != "duplicate_of"} for i in (10, 11, 12)]
    result = _verify()
    assert result.status == "fail"
    assert "has 0 event(s) folded into it, and 3 incident(s) were admitted" in result.reason


def test_an_anchor_whose_chat_post_failed_counts_as_admitted(feed) -> None:
    feed.rows = [_row(10, "undelivered"), _row(11, "folded", duplicate_of=10), _row(12, "folded", duplicate_of=10)]
    assert _verify().status == "pass"


def test_the_anchor_is_the_newest_admitted_row(feed) -> None:
    # The previous repetition folded its burst; this one did not.
    feed.rows = [
        _row(1, "injected", minutes_ago=6),
        _row(2, "folded", minutes_ago=6, duplicate_of=1),
        _row(3, "folded", minutes_ago=6, duplicate_of=1),
        _row(10, "injected"),
        _row(11, "injected"),
    ]
    result = _verify()
    assert result.status == "fail"
    assert "ledger row 11" in result.reason


def test_a_drift_record_for_the_same_name_is_not_the_anchor(feed) -> None:
    # The drift detector records the stack's own apply of the Deployment, under
    # the workload's name and kind `deployments`, sometimes after the anchor.
    feed.rows = [
        _row(10, "injected"),
        _row(11, "folded", duplicate_of=10),
        _row(12, "injected", kind="deployments", reason="OutOfBandChange"),
        _row(13, "folded", duplicate_of=10),
    ]
    result = _verify()
    assert result.status == "pass", result.reason
    assert "ledger row 10" in result.reason


def test_an_incident_from_before_this_run_fails(feed) -> None:
    feed.rows = [_row(1, "injected", minutes_ago=60), _row(2, "folded", minutes_ago=60, duplicate_of=1),
                 _row(3, "folded", minutes_ago=60, duplicate_of=1)]
    result = _verify()
    assert result.status == "fail"
    assert "older than this run's window" in result.reason


def test_no_admitted_incident_fails(feed) -> None:
    feed.rows = [_row(10, "suppressed")]
    result = _verify()
    assert result.status == "fail"
    assert "no admitted incident" in result.reason


def test_rows_for_another_workload_are_not_read(feed) -> None:
    feed.rows = [
        _row(10, "injected"),
        _row(11, "folded", duplicate_of=10),
        _row(12, "folded", duplicate_of=10),
        _row(13, "injected", name="other-workload"),
        _row(14, "injected", namespace="other-ns"),
    ]
    assert _verify().status == "pass"


def test_a_refused_read_is_an_error(feed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_KV_API_KEY", "wrong")
    result = _verify()
    assert result.status == "error"
    assert "the event ledger could not be read" in result.reason


def test_an_unreadable_pod_is_an_error(feed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "agent_shell", lambda s, t: "")
    result = _verify()
    assert result.status == "error"
    assert "could not be read" in result.reason


def test_a_run_with_no_start_time_is_an_error(feed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript, "get", lambda: SimpleNamespace(started_at=None))
    assert _verify().status == "error"


# --- the contract with the server -------------------------------------------


def test_the_admitted_statuses_are_the_servers() -> None:
    text = SERVER.read_text()
    for constant in ("FEED_STATUS_INJECTED", "FEED_STATUS_UNDELIVERED"):
        value = re.search(rf'^{constant} = "([a-z]+)"$', text, re.M)
        assert value is not None, constant
        assert value.group(1) in incidents.ADMITTED_STATUSES
    assert len(incidents.ADMITTED_STATUSES) == 2


def test_the_page_is_the_servers_largest() -> None:
    value = re.search(r"^FEED_EVENTS_MAX_LIMIT = (\d+)$", SERVER.read_text(), re.M)
    assert value is not None
    assert incidents.ROW_LIMIT == int(value.group(1))


def test_the_feed_carries_every_field_the_verdict_reads() -> None:
    text = SERVER.read_text()
    feed = text[text.index("def list_intercepted_events"):]
    feed = feed[: feed.index("\ndef ")]
    for field in incidents.ROW_FIELDS:
        assert f'"{field}":' in feed, field


# --- the case and registration ----------------------------------------------


def test_the_case_folds_all_but_one_of_the_stacks_replicas() -> None:
    task = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])
    variables = task["infrastructure"]["variables"]
    check = _spec_entries()["replicas-folded-into-one-incident"]["check"]
    assert check["namespace"] == variables["incident_namespace"]
    assert check["name"] == variables["workload_name"]
    assert check["min_folded"] == variables["replicas"] - 1 >= 1


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["workload_incident_folded"] == "kube_agents_bench.incidents:WorkloadIncidentFoldedVerifier"
    assert VERIFIERS.get("workload_incident_folded") is WorkloadIncidentFoldedVerifier


@pytest.mark.parametrize(
    "check",
    [
        {"type": "workload_incident_folded", "namespace": "", "name": "w", "min_folded": 1},
        {"type": "workload_incident_folded", "namespace": "n", "name": "", "min_folded": 1},
        {"type": "workload_incident_folded", "namespace": "n", "name": "w", "min_folded": 0},
        {"type": "workload_incident_folded", "namespace": "n", "name": "w", "min_folded": 1, "max_age_sec": 0},
    ],
)
def test_a_malformed_check_is_rejected_at_load(check: dict[str, Any]) -> None:
    with pytest.raises(Exception):
        parse_node(check)
