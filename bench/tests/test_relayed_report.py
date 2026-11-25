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

"""The reply-under-relayed-report case (relayed_report, #6870): its prompt,
the run's sequence over a fake transport, how failures map, the harness's
dispatch, and the inject door's transport against a stub door."""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from devops_bench.agents import AgentResult

from kube_agents_bench import cases, harness, relayed_report
from kube_agents_bench import inject_transport as inject
from kube_agents_bench.relayed_report import RelayTurn, ReportPost

DIRECTIVE = relayed_report.DIRECTIVE
PROMPT = f"""{DIRECTIVE}
opening: Check node pool capacity on seeded-a and report back.
reply1: Which pool did that report say was close to full?
fact1: np-quartz-7
reply2: And what share of its allocatable CPU was in use?
fact2: 92%
"""
REPORT = "Capacity watch: node pool np-quartz-7 in seeded-a is at 92% allocatable CPU."
POST = ReportPost("fake:c", REPORT, position="4", evidence="flagged report")


def _result(answer: str, *, errors: list[str] | None = None, name: str = "kubectl") -> AgentResult:
    return AgentResult(
        output=answer,
        trajectory=[{"name": name, "args": {}, "status": "completed"}],
        tokens={"input": 10, "output": 5},
        errors=errors or [],
        metadata={"final_message": answer},
    )


class FakeTransport:
    """RelayTransport, scripted: each call is recorded in order."""

    name = "fake"

    def __init__(
        self,
        *,
        answers: tuple[str, str] = ("It was np-quartz-7.", "It was at **92%** allocatable CPU."),
        post: ReportPost | None = POST,
        sessions: tuple[str, str] = ("pod-a", "pod-a"),
        fail: str = "",
    ) -> None:
        self.answers = list(answers)
        self.sessions = list(sessions)
        self.post = post
        self.fail = fail
        self.calls: list[tuple[str, str, Any]] = []

    def _maybe_fail(self, call: str) -> None:
        if self.fail == call:
            raise relayed_report.RelayUnavailable(f"{call} could not reach the door")

    def open_turn(self, conversation: str, text: str) -> RelayTurn:
        self.calls.append(("open_turn", conversation, text))
        self._maybe_fail("open_turn")
        return RelayTurn(_result("Card started; I'll report back.", name="board"), "fake:c", "task-0", "pod-a")

    def wait_for_report_post(self, conversation: str, timeout: float) -> ReportPost | None:
        self.calls.append(("wait_for_report_post", conversation, timeout))
        self._maybe_fail("wait_for_report_post")
        return self.post

    def reply(self, conversation: str, text: str) -> RelayTurn:
        self.calls.append(("reply", conversation, text))
        self._maybe_fail("reply")
        n = sum(1 for c in self.calls if c[0] == "reply")
        return RelayTurn(_result(self.answers[n - 1]), "fake:c", f"task-{n}", self.sessions[n - 1])


def _entry(result: AgentResult, name: str) -> dict[str, Any]:
    return next(e for e in result.trajectory if e.get("name") == name)


# --- the prompt --------------------------------------------------------------


def test_an_ordinary_ask_is_not_a_relayed_report_case():
    assert relayed_report.parse("check the pods") is None
    assert relayed_report.parse(f"check\n{DIRECTIVE}") is None


def test_the_five_fields_parse():
    assert relayed_report.parse(PROMPT) == relayed_report.Relayed(
        opening="Check node pool capacity on seeded-a and report back.",
        reply1="Which pool did that report say was close to full?",
        fact1="np-quartz-7",
        reply2="And what share of its allocatable CPU was in use?",
        fact2="92%",
    )


@pytest.mark.parametrize(
    "prompt",
    [
        f"{DIRECTIVE} opening: x\nreply1: a\nfact1: b\nreply2: c\nfact2: d",
        f"{DIRECTIVE}\nopening: x\nreply1: a\nfact1: b\nreply2: c",
        f"{DIRECTIVE}\nopening: x\nreply1: a\nfact1: b\nreply2: c\nfact2: d\nfact2: e",
        f"{DIRECTIVE}\nopening: x\nreply1: a\nfact1: b\nreply2: c\nfact2: d\nreport: e",
        f"{DIRECTIVE}\nopening: x\nreply1: a\nfact1: b\nreply2: c\nfact2: d\nloose words",
        f"{DIRECTIVE}\nopening: x\nreply1: a\nfact1:\nreply2: c\nfact2: d",
    ],
)
def test_a_malformed_prompt_is_refused(prompt):
    with pytest.raises(ValueError):
        relayed_report.parse(prompt)


def test_a_fact_matches_as_report_contains_would():
    assert relayed_report.carries("Pool **NP-Quartz-7** is\nfull", "np-quartz-7 is full")
    assert not relayed_report.carries("np-quartz-8", "np-quartz-7")
    assert not relayed_report.carries("anything", "  ")


# --- the run over a fake transport ------------------------------------------


def test_the_happy_path_writes_both_entries_and_merges_both_answers():
    transport = FakeTransport()
    got = relayed_report.run(transport, relayed_report.parse(PROMPT), "run/case/1")
    assert [c[0] for c in transport.calls] == ["open_turn", "wait_for_report_post", "reply", "reply"]
    assert all(c[1] == "run/case/1" for c in transport.calls)
    assert transport.calls[1][2] == relayed_report.REPORT_POST_WAIT_SECONDS
    assert [c[2] for c in transport.calls if c[0] == "reply"] == [
        "Which pool did that report say was close to full?",
        "And what share of its allocatable CPU was in use?",
    ]
    posted = _entry(got, relayed_report.POSTED_ENTRY)
    assert posted["status"] == "completed"
    assert posted["result"]["facts_in_post"] == [True, True]
    replies = _entry(got, relayed_report.REPLIES_ENTRY)
    assert replies["status"] == "completed", replies
    assert [r["fact_found"] for r in replies["args"]["replies"]] == [True, True]
    assert "np-quartz-7" in got.output and "92%" in got.output
    assert "np-quartz-7" in got.metadata["final_message"] and "92%" in got.metadata["final_message"]
    assert got.metadata["relayed_report"]["opening_final_message"].startswith("Card started")
    assert got.tokens == {"input": 30, "output": 15}
    names = [e["name"] for e in got.trajectory]
    assert names == ["board", relayed_report.POSTED_ENTRY, "kubectl", "kubectl", relayed_report.REPLIES_ENTRY]
    assert not got.errors


def test_no_report_post_is_infrastructure_and_no_reply_is_sent():
    transport = FakeTransport(post=None)
    with pytest.raises(relayed_report.RelayInfrastructure, match="never ran"):
        relayed_report.run(transport, relayed_report.parse(PROMPT), "run/case/1")
    assert [c[0] for c in transport.calls] == ["open_turn", "wait_for_report_post"]


@pytest.mark.parametrize("call", ["open_turn", "wait_for_report_post", "reply"])
def test_a_transport_failure_is_infrastructure(call):
    with pytest.raises(relayed_report.RelayInfrastructure, match="fake"):
        relayed_report.run(FakeTransport(fail=call), relayed_report.parse(PROMPT), "run/case/1")


@pytest.mark.parametrize(
    ("answers", "missing"),
    [
        (("I don't know which pool.", "It was at 92%."), "reply1"),
        (("It was np-quartz-7.", "I no longer have that report."), "reply2"),
        (("It was np-quartz-7.", ""), "reply2"),
    ],
)
def test_a_reply_missing_its_fact_is_a_graded_failure(answers, missing):
    got = relayed_report.run(FakeTransport(answers=answers), relayed_report.parse(PROMPT), "run/case/1")
    assert not got.errors  # graded, not infrastructure
    assert _entry(got, relayed_report.POSTED_ENTRY)["status"] == "completed"
    replies = _entry(got, relayed_report.REPLIES_ENTRY)
    assert replies["status"] == "error"
    assert missing in replies["result"]
    other = "reply2" if missing == "reply1" else "reply1"
    assert other not in replies["result"]


def test_replies_on_different_sessions_are_infrastructure():
    with pytest.raises(relayed_report.RelayInfrastructure, match="session"):
        relayed_report.run(FakeTransport(sessions=("pod-a", "pod-b")), relayed_report.parse(PROMPT), "run/case/1")


def test_a_transport_that_cannot_name_the_session_is_not_held_to_it():
    got = relayed_report.run(FakeTransport(sessions=("", "pod-b")), relayed_report.parse(PROMPT), "run/case/1")
    assert _entry(got, relayed_report.REPLIES_ENTRY)["status"] == "completed"


def test_the_fake_satisfies_the_seam():
    assert isinstance(FakeTransport(), relayed_report.RelayTransport)


def test_both_entries_are_harness_written():
    assert relayed_report.POSTED_ENTRY in cases.HARNESS_WRITTEN_ENTRIES
    assert relayed_report.REPLIES_ENTRY in cases.HARNESS_WRITTEN_ENTRIES


# --- the harness's dispatch --------------------------------------------------


@pytest.mark.parametrize("transport", ["api", "slack"])
def test_on_a_lane_no_transport_serves_the_run_is_infrastructure_and_sends_nothing(monkeypatch, transport):
    monkeypatch.setenv("AGENT_TRANSPORT", transport)
    monkeypatch.setenv("AGENT_INJECT_TOKEN", "tok")
    monkeypatch.setenv("AGENT_INJECT_URL", "http://door")
    sent: list[Any] = []
    monkeypatch.setattr(inject, "_request", lambda *a, **k: sent.append(a) or {})
    got = harness.KubeAgentsHarness()._execute(PROMPT)
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "no relay transport serves" in got.errors[0]
    assert got.output == ""
    assert sent == []


def test_the_inject_lane_runs_on_the_inject_transport(monkeypatch):
    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    transport = harness._relay_transport(harness.KubeAgentsHarness(), "r", "c", "1")
    assert isinstance(transport, harness._InjectRelayTransport)
    assert isinstance(transport, relayed_report.RelayTransport)


def test_a_malformed_prompt_errors_before_the_transport_is_looked_up(monkeypatch):
    monkeypatch.setattr(harness, "_relay_transport", lambda *a: pytest.fail("looked up"))
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\nopening: x")
    assert got.errors and "missing" in got.errors[0]
    assert not got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)


def test_a_configured_transport_runs_the_case(monkeypatch):
    monkeypatch.setattr(harness, "_relay_transport", lambda *a: FakeTransport())
    monkeypatch.setattr(harness, "_write_eval_run", lambda record: None)
    got = harness.KubeAgentsHarness()._execute(PROMPT)
    assert not got.errors, got.errors
    assert _entry(got, relayed_report.REPLIES_ENTRY)["status"] == "completed"


def test_a_configured_transport_without_a_post_is_infrastructure(monkeypatch):
    monkeypatch.setattr(harness, "_relay_transport", lambda *a: FakeTransport(post=None))
    monkeypatch.setattr(harness, "_write_eval_run", lambda record: None)
    got = harness.KubeAgentsHarness()._execute(PROMPT)
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "never ran" in got.errors[0]


def test_the_directive_is_known_to_the_guard():
    assert harness._unknown_directive(PROMPT) == ""
    assert relayed_report.DIRECTIVE in harness._KNOWN_DIRECTIVES
    assert harness._unknown_directive("[bench:reply-under-relayed-reprot]\nopening: x") != ""


# --- the inject transport against a stub door --------------------------------
#
# The door's two routes as a2a/gateway/inject.go serves them, per
# conversation: a POST to /inject mints a task and (here, at once) the task's
# marker, its answer post and its terminal; GET /conversations/<key> serves
# the entries after `after`, with a probe carrying the session record's
# contextId. The inject notify route's report is a post marked `report` with
# `replyTo` naming the task whose turn filed the card (PostNotifyFrame),
# appended once the opening turn's answer has been read.

TOKEN = "stub-inject-token"
RUN_ID = "run-relay"
CASE_ID = "case-relay"
REPETITION = "1"
KEY = f"{RUN_ID}/{CASE_ID}/{REPETITION}"
CONVERSATION = "inject:" + KEY
OPENING_ANSWER = "Card filed; I'll report back here."
REPLY_ANSWERS = ("The report named np-quartz-7.", "It was at 92% allocatable CPU.")


class _StubDoorHandler(BaseHTTPRequestHandler):
    server: _StubDoor

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if self.headers.get(inject.AUTHORIZATION_HEADER, "") == inject.BEARER_PREFIX + TOKEN:
            return True
        self._respond(401, {"error": "the inject door requires a bearer token"})
        return False

    def do_POST(self) -> None:
        if not self._authorized():
            return
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if urlparse(self.path).path.endswith(inject.CANCEL_SUFFIX):
            self.server.cancels.append(request)
            self._respond(200, {"conversation": CONVERSATION, "entries": [], "cancelPublished": True})
            return
        with self.server.lock:
            body = self.server.submit(request)
        self._respond(200, body)

    def do_GET(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        key = unquote(parsed.path[len(inject.CONVERSATIONS_PATH):])
        after = int(query.get("after", ["0"])[0])
        with self.server.lock:
            self.server.reads.append({"conversation": key, "probe": inject.PROBE_PARAM in query, "task": query.get("task", [""])[0]})
            entries = self.server.transcripts.get(key, [])
            fresh = [e for e in entries if e["seq"] > after]
            body: dict[str, Any] = {
                "conversation": key,
                "entries": fresh,
                "lastSeq": entries[-1]["seq"] if entries else 0,
            }
            if inject.PROBE_PARAM in query:
                probe: dict[str, Any] = {"backend": "inject", "injectOnly": True, "graceSeconds": 1, "active": False}
                if self.server.context_ids.get(key):
                    probe["contextId"] = self.server.context_ids[key]
                body["probe"] = probe
        self._respond(200, body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _StubDoor(ThreadingHTTPServer):
    """Scripted per test: the answers each turn posts, the contextId the
    record reports after each turn, and the relayed entries the notify route
    appends once the opening turn has ended."""

    def setup(self) -> None:
        self.lock = threading.Lock()
        self.transcripts: dict[str, list[dict[str, Any]]] = {}
        self.context_ids: dict[str, str] = {}
        self.submissions: list[dict[str, Any]] = []
        self.cancels: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.tasks: dict[str, str] = {}
        self.answers = [OPENING_ANSWER, *REPLY_ANSWERS]
        self.sessions = ["ctx-a", "ctx-a", "ctx-a"]
        # (conversation, replyTo or None for the opening's task) per relayed
        # report post, appended after the opening turn's terminal.
        self.relayed: list[tuple[str, str | None]] = [(CONVERSATION, None)]

    def _append(self, conversation: str, kind: str, **fields: Any) -> None:
        entries = self.transcripts.setdefault(conversation, [])
        seq = entries[-1]["seq"] + 1 if entries else 1
        entries.append({"seq": seq, "kind": kind, "ts": "2026-11-25T00:00:00Z", **fields})

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        conversation = str(request.get("conversation") or "")
        if not conversation.startswith("inject:"):
            conversation = "inject:" + conversation
        message_id = str(request.get("messageId") or "")
        if message_id in self.tasks:
            return {"conversation": conversation, "taskId": self.tasks[message_id], "accepted": True,
                    "deduplicated": True, "entries": [], "firstEventGraceSeconds": 1}
        self.submissions.append(request)
        turn = len(self.submissions) - 1
        task_id = f"task-{turn}"
        self.tasks[message_id] = task_id
        self._append(conversation, inject.ENTRY_TASK, taskId=task_id)
        self._append(conversation, inject.ENTRY_POST, text=self.answers[turn], messageId=f"inj-{turn}-answer")
        self._append(conversation, inject.ENTRY_TERMINAL, taskId=task_id, state="completed",
                     source=inject.TERMINAL_SOURCE_EXECUTOR)
        self.context_ids[conversation] = self.sessions[turn]
        if turn == 0:
            for target, reply_to in self.relayed:
                root = reply_to if reply_to is not None else task_id
                # A card's progress line comes first on the same thread,
                # relayed but not kept: it is not the report to wait for.
                self._append(target, inject.ENTRY_POST, text="card started", messageId=f"inj-progress-{target}",
                             replyTo=root, report=True)
                self._append(target, inject.ENTRY_POST, text=REPORT, messageId=f"inj-relay-{target}",
                             replyTo=root, report=True, kept=True)
        return {"conversation": conversation, "taskId": task_id, "accepted": True, "messageId": message_id,
                "entries": [], "firstEventGraceSeconds": 1}


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> Generator[_StubDoor, None, None]:
    server = _StubDoor(("127.0.0.1", 0), _StubDoorHandler)
    server.setup()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    monkeypatch.setenv("AGENT_INJECT_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AGENT_INJECT_TOKEN", TOKEN)
    monkeypatch.setenv("EVAL_CASE_ID", CASE_ID)
    monkeypatch.setenv("EVAL_REPETITION", REPETITION)
    monkeypatch.setattr(harness, "_mint_run_id", lambda: RUN_ID)
    monkeypatch.setattr(harness, "_write_eval_run", lambda record: None)
    monkeypatch.setattr(inject, "POLL_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(inject, "GRACE_MARGIN_SECONDS", 0.5)
    monkeypatch.setattr(inject, "FINISHED_SETTLE_SECONDS", 0.5)
    # The bound the run waits for the report, shrunk: a stub that never
    # relays one should not hold the test for fifteen minutes.
    monkeypatch.setattr(relayed_report, "REPORT_POST_WAIT_SECONDS", 1.5)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _run(door: _StubDoor) -> AgentResult:
    return harness.KubeAgentsHarness()._execute(PROMPT)


def _infra(result: AgentResult) -> bool:
    return bool(result.errors) and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)


def test_inject_the_green_path_opens_waits_for_the_report_and_replies_twice(door):
    got = _run(door)
    assert not got.errors, got.errors
    # Three turns on one conversation, the replies under their own message ids.
    assert [s["text"] for s in door.submissions] == [
        "Check node pool capacity on seeded-a and report back.",
        "Which pool did that report say was close to full?",
        "And what share of its allocatable CPU was in use?",
    ]
    assert {s["conversation"] for s in door.submissions[1:]} == {CONVERSATION}
    ids = [s["messageId"] for s in door.submissions]
    assert ids[1] == ids[0] + relayed_report.REPLY_SUFFIX + "1"
    assert ids[2] == ids[0] + relayed_report.REPLY_SUFFIX + "2"
    posted = _entry(got, relayed_report.POSTED_ENTRY)
    assert posted["status"] == "completed"
    assert posted["args"] == {"transport": "inject", "conversation": CONVERSATION}
    assert posted["result"]["facts_in_post"] == [True, True]
    assert "replyTo task-0" in posted["result"]["evidence"]
    assert _entry(got, relayed_report.REPLIES_ENTRY)["status"] == "completed"
    assert got.output == "\n\n".join(REPLY_ANSWERS)
    # The report is never read as a reply: neither answer carries its text.
    assert REPORT not in got.output
    assert got.metadata["relayed_report"]["opening_final_message"] == OPENING_ANSWER
    assert got.metadata["relayed_report"]["reply_task_ids"] == ["task-1", "task-2"]
    # The report wait's reads start nothing and probe nothing.
    waits = [r for r in door.reads if not r["probe"]]
    assert waits and all(r["conversation"] == CONVERSATION and r["task"] == "" for r in waits)


def test_inject_no_report_within_the_bound_is_infrastructure_and_sends_no_reply(door):
    door.relayed = []
    got = _run(door)
    assert _infra(got), got.errors
    assert "never ran" in got.errors[0]
    assert len(door.submissions) == 1


def test_inject_a_report_on_another_conversation_is_not_this_ones(door):
    door.relayed = [("inject:run-other/case-relay/1", None)]
    got = _run(door)
    assert _infra(got), got.errors
    assert "never ran" in got.errors[0]
    assert len(door.submissions) == 1
    # Every read is of this run's conversation (the preflight's before the
    # door handed back its prefixed spelling).
    assert {r["conversation"] for r in door.reads} <= {KEY, CONVERSATION}


def test_inject_a_report_about_another_task_is_not_the_openings(door):
    door.relayed = [(CONVERSATION, "task-elsewhere")]
    got = _run(door)
    assert _infra(got), got.errors
    assert len(door.submissions) == 1


def test_inject_replies_on_different_sessions_are_infrastructure(door):
    door.sessions = ["ctx-a", "ctx-a", "ctx-b"]
    got = _run(door)
    assert _infra(got), got.errors
    assert "session ctx-b" in got.errors[0] and "ctx-a" in got.errors[0]


def test_inject_a_reply_missing_its_fact_is_graded(door):
    door.answers = [OPENING_ANSWER, REPLY_ANSWERS[0], "I no longer have that report."]
    got = _run(door)
    assert not got.errors, got.errors
    replies = _entry(got, relayed_report.REPLIES_ENTRY)
    assert replies["status"] == "error" and "reply2" in replies["result"]


def test_inject_an_unreachable_door_during_the_wait_is_infrastructure(door, monkeypatch):
    def _lost(self, root, deadline):
        raise inject.InjectUnavailable("connection refused", retryable=False)

    monkeypatch.setattr(inject.InjectTask, "await_relayed_report", _lost)
    got = _run(door)
    assert _infra(got), got.errors
    assert "wait for the report post failed" in got.errors[0]
    assert len(door.submissions) == 1
