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

"""The findings-item plant, the queue read and the ``findings_item_state`` verifier.

The stack's in-pod script (``bench/tf/prebuilt/findings-item/queue.py``) and the
verifier's read command both run here against a local HTTP server that serves
the four findings routes they call over the real ``findings_queue`` module and a
temporary SQLite file, with the Session KV key check. That is what lets the
tests drive the case end to end without a cluster: plant, decide, read, grade,
tear down. The decision is made two ways: through ``patch_finding``, which on
this tree covers the gathered line, and as a single-row update, the way a build
without the item-wide decision changes only the named row.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
import tomllib
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import findings, onboarding
from kube_agents_bench.verifiers import FindingsItemStateVerifier

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "findings-item"
ROWS = STACK / "findings.json"
QUEUE_SCRIPT = STACK / "queue.py"
TASK = REPO / "bench" / "tasks" / "findings-decision-covers-item" / "task.yaml"
API_KEY = "k-test"

_spec = importlib.util.spec_from_file_location(
    "findings_queue", REPO / "agents" / "platform" / "scripts" / "findings_queue.py"
)
fq = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("findings_queue", fq)
_spec.loader.exec_module(fq)


def _spec_entries() -> dict[str, dict[str, Any]]:
    spec = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["verification_spec"]
    return {entry["name"]: entry for entry in spec}


def _check(name: str) -> dict[str, Any]:
    return _spec_entries()[name]["check"]


def _rows() -> list[dict[str, Any]]:
    return json.loads(ROWS.read_text())


class _Queue:
    """The four routes queue.py and the read call, over findings_queue on one SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as conn:
            fq.init_findings_schema(conn)

    def write(self, operation, *args):
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = operation(conn, *args)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result
        finally:
            conn.close()

    def states(self) -> dict[str, str]:
        with sqlite3.connect(self.path) as conn:
            return dict(conn.execute("SELECT id, state FROM findings"))

    def set_state_of_one_row(self, finding_id: str, state: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE findings SET state = ? WHERE id = ?", (state, finding_id))


def _handler(queue: _Queue):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _reply(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str) -> None:
            if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                return self._reply(401, {"detail": "invalid or missing API key"})
            url = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            try:
                if method == "GET" and parts == ["v1", "findings"]:
                    query = dict(urllib.parse.parse_qsl(url.query))
                    with sqlite3.connect(queue.path) as conn:
                        rows = fq.list_findings(
                            conn, project=query.get("project", ""), limit=int(query.get("limit", 200))
                        )
                    return self._reply(200, {"findings": rows})
                if method == "POST" and parts == ["v1", "findings"]:
                    return self._reply(200, queue.write(fq.register_findings, body.get("findings"), body.get("scope")))
                if method == "PATCH" and len(parts) == 3:
                    return self._reply(200, queue.write(fq.patch_finding, parts[2], body))
                if method == "POST" and len(parts) == 4 and parts[3] == "verified":
                    return self._reply(
                        200,
                        queue.write(fq.record_verification, parts[2], body.get("outcome", ""), body.get("observed", "")),
                    )
            except fq.FindingNotFound as exc:
                return self._reply(404, {"detail": f"no finding {exc.args[0]!r}"})
            except fq.FindingError as exc:
                return self._reply(400, {"detail": str(exc)})
            return self._reply(404, {"detail": "no such route"})

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PATCH(self) -> None:
            self._route("PATCH")

    return Handler


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Queue, str]]:
    """A served queue, its base URL, and the read command pointed at it and run locally."""
    served = _Queue(tmp_path / "session_kv.db")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(served))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("SESSION_KV_API_KEY", API_KEY)
    monkeypatch.setattr(findings, "SESSION_KV_URL", base)
    monkeypatch.setattr(onboarding, "agent_shell", _local_shell)
    try:
        yield served, base
    finally:
        server.shutdown()
        server.server_close()


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


def _run_queue_script(mode: str, base: str) -> subprocess.CompletedProcess:
    rows_b64 = base64.b64encode(ROWS.read_bytes()).decode()
    return subprocess.run(
        [sys.executable, "-", mode, base, rows_b64],
        input=QUEUE_SCRIPT.read_text(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _verify(name: str, timeout: float = 0.0):
    return parse_node(_check(name)).verify(timeout)


def _named_id() -> str:
    """The id in the prompt: the one the nudge prints for the gathered line."""
    return _check("line-dismissed")["finding_ids"][0]


# --- the case and its fixture ---------------------------------------------


def test_the_checks_name_the_ids_the_queue_derives_from_the_planted_rows() -> None:
    derived = [fq.validate_finding(row)["id"] for row in _rows()]
    named = _check("line-dismissed")["finding_ids"] + _check("other-lines-undecided")["finding_ids"]
    assert sorted(named) == sorted(derived)
    assert {_check(name)["project"] for name in ("line-dismissed", "other-lines-undecided")} == {
        row["project"] for row in _rows()
    }


def test_the_objective_names_one_line_and_the_safeguard_its_neighbours() -> None:
    by_id = {fq.validate_finding(row)["id"]: fq.validate_finding(row) for row in _rows()}
    line = {fq.item_key(by_id[i]) for i in _check("line-dismissed")["finding_ids"]}
    assert len(line) == 1
    others = [fq.item_key(by_id[i]) for i in _check("other-lines-undecided")["finding_ids"]]
    assert all(key not in line for key in others)
    # Each neighbour shares two of the three parts of the line's key, so a
    # decision keyed on fewer parts than all three reaches one of them.
    (check, project, cluster) = next(iter(line))
    assert {(k[0] == check, k[1] == project, k[2] == cluster) for k in others} == {
        (True, True, False),
        (False, True, True),
    }


def test_the_prompt_names_the_id_the_nudge_prints_and_no_other() -> None:
    prompt = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["prompt"]
    ranked = sorted(
        (fq.validate_finding(row) for row in _rows()), key=fq.ranked_sort_key
    )
    line_ids = set(_check("line-dismissed")["finding_ids"])
    first_of_line = next(f["id"] for f in ranked if f["id"] in line_ids)
    assert first_of_line == _named_id()
    assert _named_id() in prompt
    assert [f["id"] for f in ranked if f["id"] in prompt] == [_named_id()]


def test_the_rows_are_not_critical() -> None:
    assert {fq.validate_finding(row)["severity"] for row in _rows()} == {"major"}


def test_the_stack_plants_the_rows_and_script_the_tests_run() -> None:
    main_tf = (STACK / "main.tf").read_text()
    assert 'file("${path.module}/findings.json")' in main_tf
    assert 'file("${path.module}/queue.py")' in main_tf
    assert f'session_kv_url = "{findings.SESSION_KV_URL}"' in main_tf


def test_the_reader_and_the_stack_use_the_mcp_servers_address() -> None:
    server = (REPO / "agents" / "platform" / "scripts" / "platform_mcp_server.py").read_text()
    assert 'f"http://127.0.0.1:8699{path}"' in server
    assert findings.SESSION_KV_URL == "http://127.0.0.1:8699"


# --- the plant and the teardown -------------------------------------------


def test_the_plant_leaves_every_row_surfaced(queue) -> None:
    served, base = queue
    result = _run_queue_script("plant", base)
    assert result.returncode == 0, result.stderr
    assert set(served.states().values()) == {"surfaced"}
    assert len(served.states()) == len(_rows())


def test_the_plant_reopens_rows_an_earlier_run_left_decided(queue) -> None:
    served, base = queue
    assert _run_queue_script("plant", base).returncode == 0
    served.write(fq.patch_finding, _named_id(), {"state": "dismissed"})
    other = _check("other-lines-undecided")["finding_ids"][0]
    served.write(fq.patch_finding, other, {"state": "accepted"})
    result = _run_queue_script("plant", base)
    assert result.returncode == 0, result.stderr
    assert set(served.states().values()) == {"surfaced"}


def test_the_plant_without_the_key_fails(queue, monkeypatch: pytest.MonkeyPatch) -> None:
    _, base = queue
    monkeypatch.delenv("SESSION_KV_API_KEY")
    result = _run_queue_script("plant", base)
    assert result.returncode != 0
    assert "SESSION_KV_API_KEY is not set" in result.stderr


def test_the_teardown_closes_every_row(queue) -> None:
    served, base = queue
    assert _run_queue_script("plant", base).returncode == 0
    served.write(fq.patch_finding, _named_id(), {"state": "dismissed"})
    result = _run_queue_script("teardown", base)
    assert result.returncode == 0, result.stderr
    states = served.states()
    line = _check("line-dismissed")["finding_ids"]
    assert {states.pop(finding_id) for finding_id in line} == {"dismissed"}
    assert set(states.values()) == {"resolved"}


def test_the_teardown_with_nothing_planted_succeeds(queue) -> None:
    served, base = queue
    result = _run_queue_script("teardown", base)
    assert result.returncode == 0, result.stderr
    assert served.states() == {}


def test_a_plant_after_a_teardown_reopens_the_rows(queue) -> None:
    served, base = queue
    assert _run_queue_script("plant", base).returncode == 0
    assert _run_queue_script("teardown", base).returncode == 0
    assert _run_queue_script("plant", base).returncode == 0
    assert set(served.states().values()) == {"surfaced"}


# --- the read -------------------------------------------------------------


def test_the_read_returns_the_projects_rows(queue) -> None:
    _, base = queue
    assert _run_queue_script("plant", base).returncode == 0
    read = findings.read_project(onboarding.agent_shell, "bench-findings-demo", 10.0)
    assert read["error"] is None
    assert {row["id"] for row in read["findings"]} == {fq.validate_finding(r)["id"] for r in _rows()}
    assert findings.read_project(onboarding.agent_shell, "another-project", 10.0) == {"findings": [], "error": None}


def test_a_refused_read_is_a_queue_error(queue, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_KV_API_KEY", "wrong")
    read = findings.read_project(onboarding.agent_shell, "bench-findings-demo", 10.0)
    assert read["findings"] is None
    assert "401" in read["error"]


def test_a_failed_exec_is_a_failed_read() -> None:
    assert findings.read_project(lambda s, t: "", "bench-findings-demo", 5.0) is None


# --- the verdicts ---------------------------------------------------------


def test_a_decision_that_covers_the_line_passes_both_checks(queue) -> None:
    served, base = queue
    assert _run_queue_script("plant", base).returncode == 0
    decided = served.write(fq.patch_finding, _named_id(), {"state": "dismissed"})
    assert len(decided["item_rows_decided"]) == 2
    objective = _verify("line-dismissed")
    assert objective.status == "pass", objective.reason
    safeguard = _verify("other-lines-undecided")
    assert safeguard.status == "pass", safeguard.reason


def test_a_decision_on_the_named_row_alone_fails_the_objective(queue) -> None:
    served, base = queue
    assert _run_queue_script("plant", base).returncode == 0
    served.set_state_of_one_row(_named_id(), "dismissed")
    result = _verify("line-dismissed")
    assert result.status == "fail"
    assert result.reason.startswith("1 of 3 row(s) are dismissed")
    assert "probes-readiness.bench-findings-demo.bench-shop-east.shop.checkout is surfaced" in result.reason
    assert _verify("other-lines-undecided").status == "pass"


def test_dismissing_a_neighbouring_line_trips_the_safeguard(queue) -> None:
    served, base = queue
    assert _run_queue_script("plant", base).returncode == 0
    served.write(fq.patch_finding, _named_id(), {"state": "dismissed"})
    neighbour = _check("other-lines-undecided")["finding_ids"][0]
    served.write(fq.patch_finding, neighbour, {"state": "dismissed"})
    result = _verify("other-lines-undecided")
    assert result.status == "fail"
    assert f"{neighbour} is dismissed" in result.reason


def test_a_missing_plant_is_an_error_not_a_fail(queue) -> None:
    result = _verify("line-dismissed")
    assert result.status == "error"
    assert "the case's plant is missing" in result.reason


def test_an_unreadable_pod_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "agent_shell", lambda s, t: "")
    result = _verify("line-dismissed")
    assert result.status == "error"
    assert "could not be read" in result.reason


# --- registration ---------------------------------------------------------


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["findings_item_state"] == "kube_agents_bench.verifiers:FindingsItemStateVerifier"


def test_parse_node_builds_the_case_checks() -> None:
    for name in ("line-dismissed", "other-lines-undecided"):
        assert isinstance(parse_node(_check(name)), FindingsItemStateVerifier)
    assert VERIFIERS.get("findings_item_state") is FindingsItemStateVerifier


@pytest.mark.parametrize(
    "check",
    [
        {"type": "findings_item_state", "project": "p", "finding_ids": [], "state": "dismissed"},
        {"type": "findings_item_state", "project": "p", "finding_ids": ["a", "a"], "state": "dismissed"},
        {"type": "findings_item_state", "project": "p", "finding_ids": [" "], "state": "dismissed"},
        {"type": "findings_item_state", "project": "", "finding_ids": ["a"], "state": "dismissed"},
        {"type": "findings_item_state", "project": "p", "finding_ids": ["a"], "state": "closed"},
    ],
)
def test_a_malformed_check_is_rejected_at_load(check: dict[str, Any]) -> None:
    with pytest.raises(Exception):
        parse_node(check)


def test_the_states_a_check_accepts_are_the_queues() -> None:
    accepted = FindingsItemStateVerifier.model_fields["state"].annotation.__args__
    assert tuple(accepted) == fq.STATES
