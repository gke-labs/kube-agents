"""The card-wait stopped ask (#5407): its directive, the board read that finds
its session, the A2A door client, and the harness's stop during the hold."""

from __future__ import annotations

import http.server
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
from kube_agents_bench import a2a_door, board, harness, stop_hook

CW = stop_hook.CARD_WAIT_DIRECTIVE
TOKEN = "door-token-must-not-leak-0123456789"


# --- the directive ------------------------------------------------------------


def test_the_card_wait_directive_parses_with_defaults():
    stop = stop_hook.parse(f"{CW}\nscale it")
    assert stop == stop_hook.Stop(
        prompt="scale it",
        observe_seconds=stop_hook.DEFAULT_OBSERVE_SECONDS,
        card_wait_deadline=stop_hook.DEFAULT_DEADLINE_SECONDS,
    )
    assert stop_hook.is_stopped_ask(f"{CW}\nx") and stop_hook.is_card_wait(f"{CW}\nx")
    assert not stop_hook.is_card_wait(f"{stop_hook.STOP_DIRECTIVE}\nx")


def test_the_card_wait_directive_takes_a_deadline_and_a_window():
    stop = stop_hook.parse(f"{CW} deadline=120 observe=600\nscale it")
    assert (stop.card_wait_deadline, stop.observe_seconds, stop.mid_turn_after) == (120.0, 600.0, None)


@pytest.mark.parametrize(
    "first",
    [f"{CW} after=3", f"{CW} deadline=0", f"{CW} deadline=1801", f"{CW} deadline=1 deadline=2", f"{CW}deadline=3"],
)
def test_a_bad_card_wait_directive_is_refused(first):
    with pytest.raises(ValueError):
        stop_hook.parse(f"{first}\nask")


# --- the board read -----------------------------------------------------------


def test_sessions_since_counts_only_api_sessions_with_cards_created_since(tmp_path):
    db = tmp_path / board.BOARD_FILE
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT, creator_session_id TEXT, created_at REAL)")
    now = 1_800_000_000.0
    conn.executemany(
        "INSERT INTO tasks VALUES (?,?,?)",
        [
            ("t1", "a2a-leftover", now - 600),  # an earlier case's
            ("t2", "a2a-this", now + 5),
            ("t3", "a2a-this", now + 9),
            ("t4", "worker-card-session", now + 5),  # not an api session
            ("t5", None, now + 5),
        ],
    )
    conn.commit()

    def shell(cmd: str, timeout: float) -> str:
        cmd = cmd.replace(board.DATA_ROOT, str(tmp_path), 1)
        return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout).stdout

    import kube_agents_bench.board as b

    hp, fp = b.HERMES_PYTHON, b.FALLBACK_PYTHON
    try:
        b.HERMES_PYTHON = b.FALLBACK_PYTHON = "python3"
        assert board.sessions_with_cards_since(shell, now - 30, 30) == ["a2a-this"]
        assert board.sessions_with_cards_since(shell, now - 3600, 30) == ["a2a-leftover", "a2a-this"]
    finally:
        b.HERMES_PYTHON, b.FALLBACK_PYTHON = hp, fp


def test_sessions_since_is_none_when_the_read_cannot_be_trusted():
    assert board.sessions_with_cards_since(lambda c, t: "no sentinel", 0.0, 1) is None
    bad = board.SESSIONS_SINCE_PRESENT + "\n" + json.dumps({"sessions": [], "error": "kanban board: locked"})
    assert board.sessions_with_cards_since(lambda c, t: bad, 0.0, 1) is None


# --- the door client -------------------------------------------------------------


class _Door(http.server.BaseHTTPRequestHandler):
    calls: list = []
    status = 200
    reply: dict = {}

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                 "caller": self.headers.get(a2a_door.CALLER_HEADER), "body": body})
        out = json.dumps({"jsonrpc": "2.0", "id": body["id"], **type(self).reply}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@pytest.fixture
def door_server():
    _Door.calls, _Door.status, _Door.reply = [], 200, {"result": {"id": "t-1", "kind": "task", "status": {"state": "working"}}}
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Door)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, a2a_door.Door(f"http://127.0.0.1:{srv.server_port}", TOKEN, timeout=5)
    srv.shutdown()


def test_the_door_client_sends_the_three_calls_with_its_token_and_caller(door_server):
    _, door = door_server
    assert door.send("scale it", message_id="m-1")["id"] == "t-1"
    door.get("t-1")
    door.cancel("t-1")
    methods = [c["body"]["method"] for c in _Door.calls]
    assert methods == ["message/send", "tasks/get", "tasks/cancel"]
    first = _Door.calls[0]
    assert first["path"] == a2a_door.RPC_PATH and first["auth"] == "Bearer " + TOKEN
    assert first["caller"] == a2a_door.DEFAULT_CALLER
    assert first["body"]["params"]["configuration"] == {"blocking": False}
    assert first["body"]["params"]["message"]["parts"] == [{"kind": "text", "text": "scale it"}]
    assert _Door.calls[2]["body"]["params"] == {"id": "t-1"}


@pytest.mark.parametrize("status,retryable", [(401, False), (503, True), (500, False)])
def test_a_door_failure_never_carries_the_token(door_server, status, retryable):
    _, door = door_server
    _Door.status, _Door.reply = status, {"error": {"code": 1, "message": "no"}}
    with pytest.raises(a2a_door.DoorUnavailable) as exc:
        door.get("t-1")
    assert exc.value.retryable is retryable
    assert TOKEN not in str(exc.value) and exc.value.__cause__ is None
    assert TOKEN not in repr(door)


def test_an_unreachable_door_never_carries_the_token():
    door = a2a_door.Door("http://127.0.0.1:9", TOKEN, timeout=1)
    with pytest.raises(a2a_door.DoorUnavailable) as exc:
        door.get("t-1")
    assert TOKEN not in str(exc.value)


def test_a_jsonrpc_error_is_a_door_error(door_server):
    _, door = door_server
    _Door.reply = {"error": {"code": -32002, "message": "task not cancelable"}}
    with pytest.raises(a2a_door.DoorError) as exc:
        door.cancel("t-1")
    assert exc.value.code == -32002


# --- the harness's stop during the hold ------------------------------------------


class _FakeDoor:
    """A door whose task reads ``states`` in order (the last repeats)."""

    def __init__(self, states, cancel_error=None, after_cancel="canceled", send_errors=()):
        self.states = list(states)
        self.cancels = 0
        self.sends = []
        self.cancel_error = cancel_error
        self.after_cancel = after_cancel
        self.send_errors = list(send_errors)

    def send(self, text, *, message_id, context_id=None):
        self.sends.append((message_id, context_id))
        if self.send_errors:
            raise self.send_errors.pop(0)
        return {"id": "t-1", "contextId": context_id, "status": {"state": "submitted"}}

    def get(self, task_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"id": task_id, "status": {"state": state, "message": {"parts": [{"kind": "text", "text": "on it"}]}}}

    def cancel(self, task_id):
        self.cancels += 1
        if self.cancel_error:
            raise self.cancel_error
        self.states = [self.after_cancel]
        return {"id": task_id}


@pytest.fixture
def card_wait(monkeypatch):
    """Run the card-wait path with a fake door and board; returns a runner."""
    monkeypatch.setenv("AGENT_A2A_DOOR_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_A2A_DOOR_URL", "http://door.invalid")
    monkeypatch.setenv("AGENT_DELEGATION_POLL_INTERVAL", "1")
    clock = [0.0]
    monkeypatch.setattr(harness.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(harness.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    seen = {}
    cleaned = {"archived": [], "purged": []}
    monkeypatch.setattr(harness, "_archive_stalled_cards", lambda ids, timeout, floor=None: cleaned["archived"].extend(ids))
    monkeypatch.setattr(harness, "_purge_card_state", lambda ids, timeout, floor=None: cleaned["purged"].extend(ids))
    monkeypatch.setattr(
        harness.board, "read_session_cards",
        lambda shell, sid, timeout, **kw: board.SessionCards(session_found=True, card_ids=["c-1"], cards={"c-1": {"status": "running"}}, source="state.db"),
    )

    def run(states, sessions, *, prompt=None, cancel_error=None, after_cancel="canceled", send_errors=()):
        door = _FakeDoor(states, cancel_error, after_cancel, send_errors)
        reads = list(sessions)
        monkeypatch.setattr(harness.a2a_door, "Door", lambda *a, **k: door)
        monkeypatch.setattr(
            harness.board, "sessions_with_cards_since",
            lambda shell, since, timeout: reads.pop(0) if len(reads) > 1 else reads[0],
        )

        def fake_stop(self, result, session_id, stop, *, send_stop, poll_interval):
            seen.update(session=session_id, sent=send_stop(), stop=stop)
            result.trajectory.append({"name": stop_hook.STOP_ENTRY, "status": "completed"})

        monkeypatch.setattr(harness.KubeAgentsHarness, "_stop_after_first_card", fake_stop)
        got = harness.KubeAgentsHarness()._execute(prompt or f"{CW} deadline=60 observe=5\nscale it")
        seen["cleaned"] = cleaned
        return got, door, seen

    return run


def _infra(result):
    return bool(result.errors) and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)


def test_the_cancel_goes_once_a_card_is_on_the_board_while_the_task_works(card_wait):
    result, door, seen = card_wait(["working", "working", "working"], [[], [], ["a2a-ctx-bus"]])
    assert door.cancels == 1
    assert seen["session"] == "a2a-ctx-bus" and seen["sent"][0] is True
    assert seen["stop"].observe_seconds == 5 and seen["stop"].mid_turn_after is None
    names = [e["name"] for e in result.trajectory]
    assert names[0] == harness._A2A_DOOR_SEND_ENTRY and harness._A2A_DOOR_STATUS_ENTRY in names
    assert result.output == "on it" and not _infra(result)


def test_the_card_wait_ignores_the_lanes_transport(card_wait, monkeypatch):
    monkeypatch.setenv("AGENT_TRANSPORT", "api")
    _, door, _ = card_wait(["working"], [["a2a-x"]])
    assert door.cancels == 1


def test_a_task_that_ends_before_any_card_is_a_setup_error_not_a_pass(card_wait):
    result, door, _ = card_wait(["working", "completed"], [[]])
    # Infrastructure, and the abandoned task is cancelled so it doesn't run on.
    assert _infra(result) and "setup" in result.errors[0] and door.cancels == 1


def test_no_card_by_the_deadline_is_a_setup_error(card_wait):
    result, door, _ = card_wait(["working"], [[]], prompt=f"{CW} deadline=10\nscale it")
    assert _infra(result) and "within 10s" in result.errors[0] and door.cancels == 1


def test_two_sessions_with_cards_since_the_send_is_a_setup_error(card_wait):
    result, door, _ = card_wait(["working"], [["a2a-a", "a2a-b"]])
    assert _infra(result) and "2 sessions" in result.errors[0] and door.cancels == 1


def test_a_refused_cancel_is_reported_to_the_stop_verdict(card_wait):
    result, door, seen = card_wait(["working"], [["a2a-x"]], cancel_error=a2a_door.DoorError(-32002, "not cancelable"))
    assert seen["sent"][0] is False and "not cancelable" in seen["sent"][1]


def test_no_token_is_infrastructure(card_wait, monkeypatch):
    monkeypatch.delenv("AGENT_A2A_DOOR_TOKEN")
    result = harness.KubeAgentsHarness()._execute(f"{CW}\nscale it")
    assert _infra(result) and "AGENT_A2A_DOOR_TOKEN" in result.errors[0]


def test_the_token_never_reaches_the_result(card_wait):
    result, _, _ = card_wait(["working"], [["a2a-x"]])
    assert TOKEN not in json.dumps({"t": result.trajectory, "e": result.errors, "m": result.metadata, "o": result.output}, default=str)


def test_a_refusal_the_door_answered_is_not_retried(card_wait):
    result, door, _ = card_wait(["working"], [[]], send_errors=[a2a_door.DoorUnavailable("401", retryable=False)])
    assert _infra(result) and len(door.sends) == 1


def test_a_jsonrpc_refusal_of_the_send_is_not_retried(card_wait):
    result, door, _ = card_wait(["working"], [[]], send_errors=[a2a_door.DoorError(-32010, "caller not mapped")])
    assert _infra(result) and len(door.sends) == 1 and "caller not mapped" in result.errors[0]


def test_a_retried_send_reuses_the_message_id_and_a_fixed_context(card_wait):
    _, door, _ = card_wait(["working"], [["a2a-x"]], send_errors=[a2a_door.DoorUnavailable("503")])
    assert len(door.sends) == 2 and door.sends[0] == door.sends[1] and door.sends[0][1]


def test_a_cancel_that_never_reaches_the_door_is_infrastructure_and_cleans_up(card_wait):
    result, door, seen = card_wait(["working"], [["a2a-x"]], cancel_error=a2a_door.DoorUnavailable("timeout"))
    assert _infra(result) and door.cancels >= 1
    assert seen["cleaned"]["purged"] == ["c-1"]


def test_a_task_that_finishes_on_its_own_after_the_cancel_is_a_setup_error(card_wait):
    result, door, _ = card_wait(["working"], [["a2a-x"]], after_cancel="completed")
    assert _infra(result) and "not canceled" in result.errors[0]


def test_an_abandoned_run_cancels_its_task_and_purges_its_cards(card_wait):
    result, door, seen = card_wait(["working"], [["a2a-a", "a2a-b"]])
    assert _infra(result) and door.cancels == 1
    assert seen["cleaned"]["archived"] and seen["cleaned"]["purged"]
