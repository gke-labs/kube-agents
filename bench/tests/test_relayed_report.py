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

from kube_agents_bench import board, cases, harness, relayed_report, worker_trajectory
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


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pause before the first reply, which a test has no use for.
    monkeypatch.setattr(relayed_report, "REPLY_SETTLE_SECONDS", 0.0)


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
        opening_answer: str = "Card started; I'll report back.",
        opening_ended: str = "",
    ) -> None:
        self.opening_answer = opening_answer
        self.opening_ended = opening_ended
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
        return RelayTurn(_result(self.opening_answer, name="board"), "fake:c", "task-0", "pod-a", self.opening_ended)

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


@pytest.mark.parametrize(
    ("answer", "fact", "found"),
    [
        ("it was 42.", "42", True),
        ("it was 420", "42", False),
        ("it was 1420", "42", False),
        ("np-quartz-70 is full", "np-quartz-7", False),
        ("(np-quartz-7)", "np-quartz-7", True),
        ("at **92%** of CPU", "92%", True),
        ("at 92% of CPU", "2%", False),
    ],
)
def test_a_fact_matches_as_a_whole_token(answer, fact, found):
    assert relayed_report.carries(answer, fact) is found


@pytest.mark.parametrize(
    ("ask", "fact"),
    [
        ("opening", "np-quartz-7"),
        ("reply1", "np-quartz-7"),
        ("reply2", "92%"),
        ("reply1", "92%"),
    ],
)
def test_a_case_whose_asks_state_a_fact_is_refused(ask, fact):
    fields = {
        "opening": "Check node pool capacity on seeded-a and report back.",
        "reply1": "Which pool did that report say was close to full?",
        "fact1": "np-quartz-7",
        "reply2": "And what share of its allocatable CPU was in use?",
        "fact2": "92%",
    }
    fields[ask] += f" Was it {fact}?"
    prompt = DIRECTIVE + "\n" + "\n".join(f"{k}: {v}" for k, v in fields.items())
    with pytest.raises(ValueError, match=f"{ask} states"):
        relayed_report.parse(prompt)


def test_a_case_whose_asks_hold_a_fact_only_inside_a_longer_token_parses():
    prompt = PROMPT.replace("report back.", "report back on pool np-quartz-70.")
    assert relayed_report.parse(prompt) is not None


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


@pytest.mark.parametrize(("sessions", "which"), [(("", "pod-a"), "first"), (("pod-a", ""), "second")])
def test_a_reply_whose_session_is_unknown_is_infrastructure(sessions, which):
    with pytest.raises(relayed_report.RelayInfrastructure, match=f"could not name the session the {which} reply"):
        relayed_report.run(FakeTransport(sessions=sessions), relayed_report.parse(PROMPT), "run/case/1")


def test_an_ended_opening_is_graded_as_the_opening_and_nothing_more_is_sent():
    transport = FakeTransport(opening_ended="the opening turn ended failed")
    got = relayed_report.run(transport, relayed_report.parse(PROMPT), "run/case/1")
    assert [c[0] for c in transport.calls] == ["open_turn"]
    assert got.errors == [f"{DIRECTIVE}: the opening turn ended failed, so no reply was sent"]
    assert not any(e.get("name") in (relayed_report.POSTED_ENTRY, relayed_report.REPLIES_ENTRY) for e in got.trajectory)


def test_an_opening_answer_carrying_a_fact_is_infrastructure_before_the_wait():
    transport = FakeTransport(opening_answer="Pool np-quartz-7 is the one to watch; card filed.")
    with pytest.raises(relayed_report.RelayInfrastructure, match="already carries 'np-quartz-7'"):
        relayed_report.run(transport, relayed_report.parse(PROMPT), "run/case/1")
    assert [c[0] for c in transport.calls] == ["open_turn"]


def test_an_opening_answer_with_a_fact_only_inside_a_longer_token_goes_on():
    transport = FakeTransport(opening_answer="Card filed for np-quartz-70; 920 nodes to read.")
    got = relayed_report.run(transport, relayed_report.parse(PROMPT), "run/case/1")
    assert _entry(got, relayed_report.REPLIES_ENTRY)["status"] == "completed"


def test_a_kept_report_carrying_neither_fact_is_infrastructure_and_no_reply_is_sent():
    blocked = ReportPost("fake:c", "Card blocked: the worker has no access to seeded-a.", position="4")
    transport = FakeTransport(post=blocked)
    with pytest.raises(relayed_report.RelayInfrastructure, match="carries neither"):
        relayed_report.run(transport, relayed_report.parse(PROMPT), "run/case/1")
    assert [c[0] for c in transport.calls] == ["open_turn", "wait_for_report_post"]


def test_a_kept_report_carrying_one_fact_is_graded():
    half = ReportPost("fake:c", "Capacity watch: node pool np-quartz-7 in seeded-a is nearly full.", position="4")
    got = relayed_report.run(FakeTransport(post=half), relayed_report.parse(PROMPT), "run/case/1")
    assert _entry(got, relayed_report.POSTED_ENTRY)["result"]["facts_in_post"] == [True, False]


def test_the_first_reply_waits_the_settle_bound_after_the_post(monkeypatch):
    monkeypatch.setattr(relayed_report, "REPLY_SETTLE_SECONDS", 0.25)
    slept: list[float] = []
    monkeypatch.setattr(relayed_report.time, "sleep", slept.append)
    relayed_report.run(FakeTransport(), relayed_report.parse(PROMPT), "run/case/1")
    assert slept == [0.25]


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
# appended once the opening turn's answer has been read. As the gateway does
# on an inject conversation (relayedReport: not thread-keyed), a later turn
# is under the kept report only when its POST names the report's root as its
# `replyTo`; a reply under no report answers without the facts. The session
# pod each turn ran on is the stub's too (harness._session_pod_of patched).

TOKEN = "stub-inject-token"
RUN_ID = "run-relay"
CASE_ID = "case-relay"
REPETITION = "1"
KEY = f"{RUN_ID}/{CASE_ID}/{REPETITION}"
CONVERSATION = "inject:" + KEY
OPENING_ANSWER = "Card filed; I'll report back here."
REPLY_ANSWERS = ("The report named np-quartz-7.", "It was at 92% allocatable CPU.")
NO_REPORT_ANSWER = "I don't have a report in front of me."


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
                if self.server.child_addressees is not None:
                    # Read only off a probe about the task being followed.
                    probe["taskId"] = self.server.last_task.get(key, "")
                    probe[inject.PROBE_CHILD_ADDRESSEES_KEY] = self.server.child_addressees
                body["probe"] = probe
        self._respond(200, body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _StubDoor(ThreadingHTTPServer):
    """Scripted per test: the answers each turn posts and the state it ends
    in, the contextId the record reports and the session pod live after
    each turn, and the relayed entries the notify route appends once the
    opening turn has ended."""

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
        self.states = ["completed", "completed", "completed"]
        # The live session pod after each turn; None is a pod read that failed.
        self.pods: list[str | None] = ["pod-a", "pod-a", "pod-a"]
        self.child_addressees: list[str] | None = None
        # The root each conversation's report was kept under, once relayed.
        self.kept: dict[str, str] = {}
        self.last_task: dict[str, str] = {}
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
        self.last_task[conversation] = task_id
        answer = self.answers[turn]
        if turn > 0 and (not self.kept.get(conversation) or request.get("replyTo") != self.kept[conversation]):
            answer = NO_REPORT_ANSWER
        self._append(conversation, inject.ENTRY_TASK, taskId=task_id)
        self._append(conversation, inject.ENTRY_POST, text=answer, messageId=f"inj-{turn}-answer")
        self._append(conversation, inject.ENTRY_TERMINAL, taskId=task_id, state=self.states[turn],
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
                self.kept[target] = root
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
    # The session pod after the latest turn, as the stub scripts it.
    monkeypatch.setattr(harness, "_session_pod_of", lambda conversation: server.pods[len(server.submissions) - 1])
    # The opening's session store: it filed a card, unless a test says not.
    server.session_cards = board.SessionCards(session_found=True, card_ids=["card-1"])
    # Any other session's cards, by session id: a sibling unit's on the
    # shared board.
    server.other_sessions = {}
    server.session_reads = []

    def _read_session_cards(shell, session_id, timeout, **kwargs):
        server.session_reads.append(session_id)
        if session_id in server.other_sessions:
            return server.other_sessions[session_id]
        return server.session_cards

    monkeypatch.setattr(harness.board, "read_session_cards", _read_session_cards)
    # The cleanup after the run: the workers' capture its fan-out is read
    # from, and what was archived and purged (the purge's fanned-out cards
    # apart).
    server.capture = None
    server.archived, server.purged, server.purged_children = [], [], []
    monkeypatch.setattr(
        harness.board, "sessions_with_cards_since", lambda *a, **k: pytest.fail("swept every session")
    )
    monkeypatch.setattr(harness.worker_trajectory, "capture", lambda shell, ids, timeout: server.capture)
    monkeypatch.setattr(harness, "_archive_stalled_cards", lambda ids, *a, **k: server.archived.extend(ids))

    def _purge(ids, timeout, fanned_out=(), floor=None):
        server.purged.extend(ids)
        server.purged_children.extend(fanned_out)

    monkeypatch.setattr(harness, "_purge_card_state", _purge)
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
    # Both replies name the opening's task, the root the report was kept
    # under; the opening names none.
    assert "replyTo" not in door.submissions[0]
    assert [s.get("replyTo") for s in door.submissions[1:]] == ["task-0", "task-0"]
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


def test_inject_replies_on_different_session_pods_are_infrastructure(door):
    door.pods = ["pod-a", "pod-a", "pod-b"]
    got = _run(door)
    assert _infra(got), got.errors
    assert "session pod-b" in got.errors[0] and "pod-a" in got.errors[0]


def test_inject_one_context_id_on_two_pods_is_still_two_sessions(door):
    # The contextId never changes on a conversation; the pod is what moved.
    door.sessions = ["ctx-a", "ctx-a", "ctx-a"]
    door.pods = ["pod-a", "pod-a", "pod-c"]
    got = _run(door)
    assert _infra(got), got.errors
    assert "session pod-c" in got.errors[0]


def test_inject_the_sessions_are_the_pods(door):
    door.sessions = ["ctx-a", "ctx-b", "ctx-c"]
    got = _run(door)
    assert not got.errors, got.errors
    assert got.metadata["relayed_report"]["sessions"] == ["pod-a", "pod-a", "pod-a"]


@pytest.mark.parametrize(("turn", "label"), [(0, "opening turn"), (1, "reply 1"), (2, "reply 2")])
def test_inject_a_pod_read_that_fails_is_infrastructure_naming_the_turn(door, turn, label):
    door.pods[turn] = None
    got = _run(door)
    assert _infra(got), got.errors
    assert f"could not be read after the {label}" in got.errors[0]


def test_inject_no_live_pod_after_a_reply_is_infrastructure(door):
    door.pods = ["pod-a", "", "pod-a"]
    got = _run(door)
    assert _infra(got), got.errors
    assert "no live session pod" in got.errors[0] and "reply 1" in got.errors[0]


def test_inject_a_reply_under_no_report_does_not_carry_the_facts(door, monkeypatch):
    # The stub attaches the kept report only on replyTo, as the gateway
    # does on an inject conversation: a transport that sent none would be
    # graded on answers with nothing to answer from.
    original = harness.KubeAgentsHarness._send_reply_turn

    def _without_reply_to(self, *args, **kwargs):
        kwargs.pop("reply_to", None)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(harness.KubeAgentsHarness, "_send_reply_turn", _without_reply_to)
    got = _run(door)
    replies = _entry(got, relayed_report.REPLIES_ENTRY)
    assert replies["status"] == "error"
    assert [s.get("replyTo") for s in door.submissions[1:]] == [None, None]


@pytest.mark.parametrize("state", ["failed", "canceled"])
def test_inject_an_opening_that_did_not_complete_is_graded_as_the_opening(door, state):
    door.states[0] = state
    got = _run(door)
    assert not _infra(got), got.errors
    assert any(f"the opening turn ended {state}, so no reply was sent" in e for e in got.errors), got.errors
    assert len(door.submissions) == 1
    assert got.output == OPENING_ANSWER


def test_inject_an_opening_that_filed_no_card_is_graded_without_the_wait(door, monkeypatch):
    door.child_addressees = []
    door.session_cards = board.SessionCards(session_found=True, card_ids=[])
    monkeypatch.setattr(relayed_report, "REPORT_POST_WAIT_SECONDS", 30.0)
    got = _run(door)
    assert not _infra(got), got.errors
    assert any("filed no card" in e for e in got.errors), got.errors
    assert len(door.submissions) == 1
    assert not [r for r in door.reads if not r["probe"]]  # no report wait


@pytest.mark.parametrize(
    ("child_addressees", "cards"),
    [
        (None, board.SessionCards(session_found=True, card_ids=[])),  # the door can't say who was delegated to
        (["platform-router"], board.SessionCards(session_found=True, card_ids=[])),  # a delegate files them
        ([], board.SessionCards(session_found=False)),  # the store doesn't know the session
        ([], None),  # the read failed
    ],
)
def test_inject_an_opening_whose_cards_cannot_be_told_waits_for_the_report(door, child_addressees, cards):
    door.child_addressees = child_addressees
    door.session_cards = cards
    got = _run(door)
    assert not got.errors, got.errors
    assert len(door.submissions) == 3


def test_inject_an_opening_answer_carrying_a_fact_is_infrastructure(door):
    door.answers = ["Card filed; np-quartz-7 looks tight already.", *REPLY_ANSWERS]
    got = _run(door)
    assert _infra(got), got.errors
    assert "already carries" in got.errors[0]
    assert len(door.submissions) == 1


def test_inject_a_kept_report_with_neither_fact_is_infrastructure(door):
    door.relayed = []
    original = door.submit

    def _blocked(request):
        body = original(request)
        if len(door.submissions) == 1:
            conversation = body["conversation"]
            door._append(conversation, inject.ENTRY_POST, text="Card blocked: no access to seeded-a.",
                         messageId="inj-relay-blocked", replyTo=body["taskId"], report=True, kept=True)
            door.kept[conversation] = body["taskId"]
        return body

    door.submit = _blocked
    got = _run(door)
    assert _infra(got), got.errors
    assert "carries neither" in got.errors[0]
    assert len(door.submissions) == 1


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


def test_inject_the_openings_card_is_cleaned_up_after_a_green_run(door):
    # The card ran to done: purged, not archived.
    door.session_cards = board.SessionCards(
        session_found=True, card_ids=["card-1"], cards={"card-1": {"status": "done"}}
    )
    got = _run(door)
    assert not got.errors, got.errors
    assert door.archived == []
    assert door.purged == ["card-1"]
    # Read from the conversation's own session, off the turns' contextId.
    assert set(door.session_reads) == {board.api_session_id("ctx-a")}


def test_inject_the_openings_card_is_archived_and_purged_when_no_report_comes(door):
    door.relayed = []
    got = _run(door)
    assert _infra(got)
    assert door.archived == ["card-1"] and door.purged == ["card-1"]



def test_inject_the_cleanup_leaves_a_sibling_sessions_cards_alone(door, monkeypatch):
    # A sibling unit in the parallel phase filed a live card on the same
    # board in this run's window: only this conversation's session is
    # cleaned, and the sibling's is never read.
    door.relayed = []
    door.other_sessions = {"a2a-sibling": board.SessionCards(session_found=True, card_ids=["sibling-card"])}
    monkeypatch.setattr(harness.board, "sessions_with_cards_since", lambda *a, **k: ["a2a-sibling", "a2a-ctx-a"])
    got = _run(door)
    assert _infra(got)
    assert "sibling-card" not in door.archived and "sibling-card" not in door.purged
    assert "a2a-sibling" not in door.session_reads
    assert door.archived == ["card-1"] and door.purged == ["card-1"]


def test_inject_the_cleanup_purges_the_cards_a_worker_fanned_out(door):
    door.session_cards = board.SessionCards(
        session_found=True, card_ids=["card-1"], cards={"card-1": {"status": "done"}}
    )
    door.capture = worker_trajectory.WorkerCapture(
        entries=[],
        summary={
            "cards": [
                {"task": "card-1", "status": "done", "fanned_out": ["child-1"]},
                {"task": "child-1", "status": "done", "fanned_out": ["grandchild-1"]},
                {"task": "grandchild-1", "status": "running", "fanned_out": []},
            ]
        },
        tokens={},
        unbilled=[],
    )
    got = _run(door)
    assert not got.errors, got.errors
    assert door.purged == ["card-1"]
    assert door.purged_children == ["child-1", "grandchild-1"]
    # A fanned-out card still running under a card that ended is archived.
    assert door.archived == ["grandchild-1"]


@pytest.mark.parametrize("context_id", ["", "not/path-safe"])
def test_inject_an_unresolved_session_cleans_nothing(door, monkeypatch, context_id):
    door.relayed = []
    door.sessions = [context_id] * 3
    got = _run(door)
    assert _infra(got)
    assert door.archived == [] and door.purged == []
    assert door.session_reads == []


def test_inject_an_opening_budget_below_the_floor_is_refused_as_configuration(door, monkeypatch):
    # The opening is the conversation's first turn: a budget below the
    # gateway's floor is refused before anything is sent, as the plain
    # inject opening refuses it, not raised to the floor.
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "0.5")
    got = _run(door)
    assert not _infra(got), got.errors
    assert got.errors[0].startswith("AGENT_INJECT_TIMEOUT: "), got.errors
    assert "below the floor" in got.errors[0]
    assert door.submissions == []


@pytest.mark.parametrize(
    "name", ["AGENT_INJECT_TIMEOUT", "AGENT_INJECT_LOCAL_PORT", "AGENT_DELEGATION_TIMEOUT", "AGENT_DELEGATION_POLL_INTERVAL"]
)
def test_inject_a_non_numeric_setting_surfaces_its_config_error(door, monkeypatch, name):
    monkeypatch.setenv(name, "soon")
    got = _run(door)
    assert not _infra(got), got.errors
    assert got.errors[0] == f"{name} must be numeric, got 'soon'"
    assert door.submissions == []


def test_inject_the_opening_writes_its_turn_timing(door, monkeypatch):
    timed: list[float] = []
    monkeypatch.setattr(harness, "_write_turn_timing", timed.append)
    got = _run(door)
    assert not got.errors, got.errors
    assert len(timed) == 1


# --- facts that are only emphasis, and identifiers with underscores -----------


@pytest.mark.parametrize("fact", ["***", "___", "``", "*_`"])
def test_a_fact_made_only_of_emphasis_marks_is_an_authoring_error(fact):
    prompt = PROMPT.replace("fact1: np-quartz-7", f"fact1: {fact}")
    assert prompt != PROMPT
    with pytest.raises(ValueError, match="only emphasis marks"):
        relayed_report.parse(prompt)


@pytest.mark.parametrize(
    ("answer", "fact", "want"),
    [
        ("The pool is gke_proj_np-7.", "gke_proj_np-7", True),
        ("The pool is **gke_proj_np-7**.", "gke_proj_np-7", True),
        ("The pool is `gke_proj_np-7`.", "gke_proj_np-7", True),
        ("The pool is gkeprojnp-7.", "gke_proj_np-7", False),
        ("The nodepool is full.", "node_pool", False),
        ("The node_pool is full.", "nodepool", False),
        ("The pool is gke_proj_np-7.", "np-7", False),
        ("The pool is _np-7_.", "np-7", True),
    ],
)
def test_an_underscore_inside_an_identifier_is_part_of_it(answer, fact, want):
    assert relayed_report.carries(answer, fact) is want
