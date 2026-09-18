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

"""Functional tests for the inject transport.

A local HTTP stub stands in for the A2A gateway's inject side door, serving
the three endpoints the real one serves (submit, the read route with its
probe, cancel) and scripted with the transcript a given task produced and
what the gateway's record shows. ``AGENT_INJECT_URL`` points the harness at
it, so nothing here spawns ``kubectl``. This exercises the full preflight ->
submit -> poll -> fold -> classify -> AgentResult path the eval harness
consumes.

The stub's replies are shaped from what the gateway actually posts
(``a2a/gateway/relay.go``): a placeholder that gets edited as the rolling
progress line, the deliverable as its own post, and a terminal entry after
both. Its probe is shaped from ``probeReport`` in ``a2a/gateway/inject.go``:
a pure read of the record, which the harness classifies. It also demands the
bearer token on every request, as the door does, and dedupes a POST on its
message id, as the door does.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from kube_agents_bench import harness
from kube_agents_bench import inject_transport as inject
from kube_agents_bench.harness import KubeAgentsHarness

# The gateway's own placeholder, which the relay then edits in place. Spelled
# here because the stub has to behave like the gateway, not because the
# transport recognises it -- the whole point of the deliverable rule is that
# the harness never matches on this string.
PLACEHOLDER = "⏳ submitted…"
PLACEHOLDER_ID = "inj-1"
RESULT_ID = "inj-2"

# The bearer token the stub requires, and what the harness is told to present.
TOKEN = "stub-inject-token"
# The gateway's first-event grace, as the stub reports it. Small, so a test
# that exercises the deadline sits above the floor with a one-second budget
# once the fixture shrinks the margin.
GRACE_SECONDS = 1
# The run, case and repetition the fixture pins, so the key and message id
# are predictable: ``run-test/case-under-test/2``.
RUN_ID = "run-test"
CASE_ID = "case-under-test"
REPETITION = "2"
CONVERSATION = f"{RUN_ID}/{CASE_ID}/{REPETITION}"
# The harness's real run-id mint, which the fixture replaces with a fixed one
# so the key and message id are predictable; the tests about minting put it
# back.
REAL_MINT_RUN_ID = harness._mint_run_id


def entry(seq: int, kind: str, **fields: Any) -> dict[str, Any]:
    """One transcript entry in the gateway's shape."""
    return {"seq": seq, "kind": kind, "ts": "2026-09-17T00:00:00Z", **fields}


def completed_transcript(task_id: str, answer: str) -> list[dict[str, Any]]:
    """The entries a task that ran and answered leaves behind."""
    return [
        entry(1, inject.ENTRY_TASK, taskId=task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_EDIT, text="⚙️ **working** — reading the fleet", messageId=PLACEHOLDER_ID),
        entry(4, inject.ENTRY_POST, text=answer, messageId=RESULT_ID),
        entry(5, inject.ENTRY_EDIT, text="✅ **completed**", messageId=PLACEHOLDER_ID),
        entry(6, inject.ENTRY_TERMINAL, taskId=task_id, state="completed",
              source=inject.TERMINAL_SOURCE_EXECUTOR),
    ]


def running_transcript(task_id: str, *posts: str) -> list[dict[str, Any]]:
    """The entries of a task an executor took and has not finished."""
    entries = [
        entry(1, inject.ENTRY_TASK, taskId=task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_EDIT, text="⚙️ **working**", messageId=PLACEHOLDER_ID),
    ]
    for i, text in enumerate(posts):
        entries.append(entry(4 + i, inject.ENTRY_POST, text=text, messageId=f"inj-{10 + i}"))
    return entries


class _StubGatewayHandler(BaseHTTPRequestHandler):
    """The inject door's three endpoints, scripted per test."""

    server: _StubGatewayServer

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        """The door refuses every unauthenticated request, so the stub does."""
        presented = self.headers.get(inject.AUTHORIZATION_HEADER, "")
        self.server.seen_authorization.append(presented)
        if presented == inject.BEARER_PREFIX + self.server.token:
            return True
        self._respond(401, {"error": "the inject door requires a bearer token"})
        return False

    def do_POST(self) -> None:
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length)) if length else {}
        parsed = urlparse(self.path)
        if parsed.path.endswith(inject.CANCEL_SUFFIX):
            self.server.cancels.append(request)
            self.server.calls.append("cancel")
            self.server.entries = self.server.entries + self.server.on_cancel
            self._respond(
                200,
                {
                    "conversation": "inject:" + CONVERSATION,
                    "entries": [],
                    "cancelPublished": self.server.cancel_published,
                    "refusal": "" if self.server.cancel_published else inject.REFUSAL_NO_CANCEL,
                    "firstEventGraceSeconds": self.server.grace_seconds,
                },
            )
            return
        self.server.submissions.append(request)
        self.server.calls.append("submit")
        attempts = len(self.server.submissions)
        if self.server.submit_status is not None and (
            self.server.submit_failures < 0 or attempts <= self.server.submit_failures
        ):
            self._respond(self.server.submit_status, {"error": "no"})
            return
        conversation = "inject:" + str(request.get("conversation") or "")
        if not self.server.accept_submissions:
            self._respond(
                200,
                {
                    "conversation": conversation,
                    "accepted": False,
                    "note": self.server.refusal_note,
                    "refusal": self.server.refusal_code,
                    "entries": [],
                    "firstEventGraceSeconds": self.server.grace_seconds,
                },
            )
            return
        # The door dedupes on the message id: a repeat is answered with the
        # task the first one started, and nothing is routed for it.
        message_id = str(request.get("messageId") or "")
        deduplicated = bool(message_id) and message_id in self.server.accepted_ids
        if message_id:
            self.server.accepted_ids.append(message_id)
        body: dict[str, Any] = {
            "conversation": conversation,
            "taskId": self.server.task_id,
            "accepted": True,
            "messageId": message_id or "inj-msg-1",
            "entries": [],
            "firstEventGraceSeconds": self.server.grace_seconds,
        }
        if deduplicated:
            body["deduplicated"] = True
        if self.server.accepted_at is None:
            self.server.accepted_at = time.monotonic()
        self._respond(200, body)

    def do_GET(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        after = int(query.get("after", ["0"])[0])
        probed = query.get(inject.PROBE_PARAM, ["0"])[0] != "0"
        task = query.get("task", [""])[0]
        failed = self.server.poll_fails()
        self.server.polls.append(
            {
                "path": parsed.path,
                "after": after,
                "task": task,
                "probe": probed,
                "wait": int(query.get("wait", ["0"])[0]),
                "failed": failed,
            }
        )
        if failed:
            self.send_error(self.server.poll_status or 503)
            return
        probe: dict[str, Any] | None = None
        if probed:
            self.server.calls.append("probe" if self.server.submissions else "preflight")
            probe = self.server.probe_report()
        fresh = [e for e in self.server.entries if e["seq"] > after]
        # The gateway serves at most what it has; a page cap is what makes the
        # `after` contract worth testing at all.
        page = fresh[: self.server.page_size] if self.server.page_size else fresh
        last_seq = page[-1]["seq"] if page else (self.server.entries[-1]["seq"] if self.server.entries else 0)
        body: dict[str, Any] = {
            "conversation": "inject:" + CONVERSATION,
            "entries": page,
            "lastSeq": last_seq,
        }
        if self.server.terminal_out_of_band and not fresh:
            body["terminal"] = self.server.terminal_out_of_band
        if probe is not None:
            body["probe"] = probe
        self._respond(200, body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep pytest output clean


class _StubGatewayServer(ThreadingHTTPServer):
    task_id: str = "task-abc123"
    entries: list[dict[str, Any]]
    submissions: list[dict[str, Any]]
    polls: list[dict[str, Any]]
    cancels: list[dict[str, Any]]
    seen_authorization: list[str]
    # Message ids the stub has accepted a POST for, which is what it dedupes on.
    accepted_ids: list[str]
    # The order the calls arrived in ("preflight", "submit", "probe",
    # "cancel"): the contracts under test are that the read precedes the
    # POST and that a cancel never precedes a read.
    calls: list[str]
    token: str = TOKEN
    # Entries the stub appends when the cancel route is called -- the gateway
    # acknowledges a cancel on the conversation, and the executor may confirm.
    on_cancel: list[dict[str, Any]]
    # What successive probed reads report as the executor's state; the last
    # repeats once the list is exhausted. Empty means "working" for as long
    # as the task is active -- what the real gateway answers for a task an
    # executor took. "" is a stream with no event.
    executor_states: list[str]
    probed_reads: int = 0
    active_reads: int = 0
    # Overrides on the probe. probe_active None derives it: active until the
    # transcript carries this task's terminal, and never before a POST.
    probe_active: bool | None = None
    probe_final: bool = False
    probe_detached: bool = False
    probe_age_seconds: int = 0
    # The fold's terminal on the read, when probe_final: whose word it is,
    # the result artifact's text, the terminal's message.
    probe_terminal_source: str = ""
    probe_result: str = ""
    probe_reason: str = ""
    probe_error: str = ""
    inject_only: bool = True
    backend: str = "inject"
    # The first-event grace the stub reports.
    grace_seconds: int = GRACE_SECONDS
    accept_submissions: bool = True
    # Whether the door reports that a cancel reached the bus. False is the
    # gateway refusing it or failing to publish it, which is a 200 too.
    cancel_published: bool = True
    refusal_note: str = "the gateway answered without starting a task"
    # The machine-readable half of a refusal, which is what the harness
    # branches on; "" is a door too old to send one.
    refusal_code: str = ""
    # Non-None makes a POST answer with that status instead; submit_failures
    # bounds how many leading POSTs do (-1: all of them).
    submit_status: int | None = None
    submit_failures: int = -1
    # Non-None makes GETs answer with that status: every GET when
    # poll_failures is -1, otherwise the first poll_failures GETs that
    # arrive poll_fail_after_seconds or more after the first accepted POST,
    # which is a tunnel dropping mid-task.
    poll_status: int | None = None
    poll_failures: int = -1
    poll_fail_after_seconds: float = 0.0
    failed_polls: int = 0
    accepted_at: float | None = None
    # Serve at most this many entries per GET; 0 means all of them.
    page_size: int = 0
    # A terminal the gateway reports out of band once the entries are drained,
    # which is how a late reader learns an answer it missed.
    terminal_out_of_band: str = ""

    def poll_fails(self) -> bool:
        """Whether this GET is one of the scripted failures."""
        if self.poll_status is None:
            return False
        if self.poll_failures < 0:
            return True
        if self.accepted_at is None or self.failed_polls >= self.poll_failures:
            return False
        if time.monotonic() - self.accepted_at < self.poll_fail_after_seconds:
            return False
        self.failed_polls += 1
        return True

    def probe_report(self) -> dict[str, Any]:
        self.probed_reads += 1
        finished = any(
            e["kind"] == inject.ENTRY_TERMINAL and e.get("taskId") == self.task_id
            for e in self.entries
        )
        active = self.probe_active
        if active is None:
            active = bool(self.submissions) and not finished
        report: dict[str, Any] = {
            "backend": self.backend,
            "injectOnly": self.inject_only,
            "graceSeconds": self.grace_seconds,
            "active": active,
        }
        if active:
            # Scripted states advance per read of the ACTIVE task, so the
            # preflight read (before any POST) does not consume one.
            self.active_reads += 1
            if self.executor_states:
                idx = min(self.active_reads - 1, len(self.executor_states) - 1)
                state = self.executor_states[idx]
            else:
                state = inject.STATE_WORKING
            report.update(
                {
                    "taskId": self.task_id,
                    "submittedAt": "2026-09-17T00:00:00Z",
                    "ageSeconds": self.probe_age_seconds,
                    "detached": self.probe_detached,
                    "executorState": state,
                    "final": self.probe_final,
                }
            )
            if self.probe_final:
                report["terminalSource"] = self.probe_terminal_source
                report["result"] = self.probe_result
                report["reason"] = self.probe_reason
            if self.entries:
                posts = [e for e in self.entries if e["kind"] == inject.ENTRY_POST]
                if posts:
                    report["lastPost"] = posts[-1]
        if self.probe_error:
            report["error"] = self.probe_error
        return report


@pytest.fixture
def stub_gateway(monkeypatch: pytest.MonkeyPatch) -> Generator[_StubGatewayServer, None, None]:
    server = _StubGatewayServer(("127.0.0.1", 0), _StubGatewayHandler)
    server.entries = []
    server.submissions = []
    server.polls = []
    server.cancels = []
    server.seen_authorization = []
    server.accepted_ids = []
    server.calls = []
    server.on_cancel = []
    server.executor_states = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    monkeypatch.setenv("AGENT_INJECT_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AGENT_INJECT_TOKEN", TOKEN)
    monkeypatch.setattr(harness, "_mint_run_id", lambda: RUN_ID)
    monkeypatch.setenv("EVAL_CASE_ID", CASE_ID)
    monkeypatch.setenv("EVAL_REPETITION", REPETITION)
    # The poll pause is dead time in a test whose stub answers instantly.
    monkeypatch.setattr(inject, "POLL_PAUSE_SECONDS", 0.01)
    # And so are the real-world margins. They are floors on how long the
    # harness waits before deciding a task is over, sized against a gateway
    # whose grace defaults to ten minutes; a test asserting what happens AT
    # the deadline should not have to sit through them.
    # test_the_budget_floor_is_the_grace_plus_a_real_margin pins the real
    # values, which is where that property belongs.
    monkeypatch.setattr(inject, "GRACE_MARGIN_SECONDS", 0.5)
    monkeypatch.setattr(inject, "CANCEL_SETTLE_SECONDS", 5.0)
    monkeypatch.setattr(inject, "FINISHED_SETTLE_SECONDS", 0.5)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def infra(result: Any) -> bool:
    return bool(result.errors) and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)


def status_entries(result: Any) -> list[tuple[str, bool]]:
    """The lifecycle the record carries, as (state, final) pairs in order."""
    return [
        (e["args"]["state"], e["args"]["final"])
        for e in result.trajectory
        if e["name"] == inject.EVENT_ENTRY_STATUS
    ]


# --------------------------------------------------------------------------
# The happy path: submit, fold, grade.


def test_the_deliverable_is_the_post_nothing_rewrote(stub_gateway: _StubGatewayServer) -> None:
    """The answer a customer reads is a post of its own; the rolling status
    line is a post the relay keeps editing. Telling them apart by "was this
    ever edited" is what keeps the harness out of the business of recognising
    the relay's prose."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "the fleet is fine")

    result = KubeAgentsHarness().run("how is the fleet?")

    assert result.output == "the fleet is fine"
    assert result.metadata["final_message"] == "the fleet is fine"
    assert result.metadata["terminal_state"] == "completed"
    assert result.metadata["task_id"] == stub_gateway.task_id
    assert not result.errors


def test_the_prompt_reaches_the_gateway_as_a_chat_message(
    stub_gateway: _StubGatewayServer,
) -> None:
    """The submission is a message from a mapped author on a conversation --
    the same three fields any other backend delivers -- keyed by run, case
    and repetition."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")

    KubeAgentsHarness().run("check the pods")

    submission = stub_gateway.submissions[0]
    assert submission["text"] == "check the pods"
    assert submission["author"] == inject.DEFAULT_AUTHOR
    assert submission["conversation"] == CONVERSATION
    assert submission["messageId"] == CONVERSATION


def test_the_transcript_stash_carries_the_answer(stub_gateway: _StubGatewayServer) -> None:
    """The text verifiers read the stash, not the AgentResult, so a transport
    whose result is right and whose stash is empty grades every case at
    zero."""
    from kube_agents_bench import transcript

    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "OOMKilled on payments-api")

    KubeAgentsHarness().run("why is it crashing?")

    snapshot = transcript.get()
    assert snapshot is not None
    assert "OOMKilled" in snapshot.output
    assert "OOMKilled" in snapshot.final_message


def test_a_paged_poll_reads_every_entry_once(stub_gateway: _StubGatewayServer) -> None:
    """The `after` contract: a reader that passes back the gateway's lastSeq
    sees every later entry exactly once. A gap loses the deliverable and a
    repeat double-counts it."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "paged answer")
    stub_gateway.page_size = 2

    result = KubeAgentsHarness().run("go")

    assert result.output == "paged answer"
    # Six entries at two per page is at least three polls, and the transport
    # must have advanced its cursor each time rather than re-reading page one.
    afters = [poll["after"] for poll in stub_gateway.polls if poll["task"]]
    assert afters == sorted(afters)
    assert len(set(afters)) > 1
    posts = [e for e in result.trajectory if e["name"] == inject.EVENT_ENTRY_POST]
    assert len(posts) == 2


def test_a_terminal_reported_out_of_band_is_still_seen(stub_gateway: _StubGatewayServer) -> None:
    """A reader that arrives after the event must learn the answer rather than
    waiting out its deadline for one that has been and gone."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_POST, text="answered before you looked", messageId=RESULT_ID),
        # The relay edits the rolling line at the terminal, always; a stub
        # that skips it leaves its own placeholder looking like an answer.
        entry(4, inject.ENTRY_EDIT, text="✅ **completed**", messageId=PLACEHOLDER_ID),
    ]
    stub_gateway.terminal_out_of_band = "completed"

    result = KubeAgentsHarness().run("late")

    assert result.metadata["terminal_state"] == "completed"
    assert result.output == "answered before you looked"


def test_the_record_carries_the_lifecycle_as_status_events(
    stub_gateway: _StubGatewayServer,
) -> None:
    """Every executor state the read route showed, and the terminal, land in
    the trajectory as a2a.status-update entries -- the same name the bus
    transport uses, and the final one is what rung 3 accepts in place of a
    token count. Tokens stay null; latency is the harness's."""
    stub_gateway.executor_states = ["submitted", "working"]
    # Let two reads see the task running before the terminal appears.
    stub_gateway.entries = running_transcript(stub_gateway.task_id)

    def finish() -> None:
        time.sleep(0.3)
        stub_gateway.entries = stub_gateway.entries + [
            entry(4, inject.ENTRY_POST, text="all quiet", messageId=RESULT_ID),
            entry(5, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="completed",
                  source=inject.TERMINAL_SOURCE_EXECUTOR),
        ]

    threading.Thread(target=finish, daemon=True).start()
    result = KubeAgentsHarness().run("watch it")

    assert result.output == "all quiet"
    assert status_entries(result) == [("submitted", False), ("working", False), ("completed", True)]
    assert result.metadata["executor_states"] == ["submitted", "working", "completed"]
    assert all(v is None for v in result.tokens.values())
    # The lifecycle is recorded once per state, however many reads saw it.
    assert stub_gateway.probed_reads >= 3


def test_the_record_stores_the_run_case_and_repetition(
    stub_gateway: _StubGatewayServer,
) -> None:
    """The gateway's ingress log joins the backend message id to the
    correlationId; the record stores the same triple, so the join reaches the
    eval record with nothing else added."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")

    result = KubeAgentsHarness().run("go")

    assert result.metadata["run_id"] == RUN_ID
    assert result.metadata["case_id"] == CASE_ID
    assert result.metadata["repetition"] == REPETITION
    assert result.metadata["message_id"] == CONVERSATION
    assert result.metadata["conversation"] == "inject:" + CONVERSATION
    assert result.metadata["inject_only"] is True
    assert result.metadata["backend"] == "inject"


def test_the_run_id_is_minted_outside_the_presubmit(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither variable is set on a developer's machine: the run id is minted
    per invocation, so two concurrent runs never share a key, and the case
    and repetition fall back to fixed names so the key stays well-formed."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")
    monkeypatch.setattr(harness, "_mint_run_id", REAL_MINT_RUN_ID)
    monkeypatch.delenv("EVAL_CASE_ID", raising=False)
    monkeypatch.delenv("EVAL_REPETITION", raising=False)

    KubeAgentsHarness().run("go")
    KubeAgentsHarness().run("go")

    first, second = (s["conversation"] for s in stub_gateway.submissions)
    assert first != second
    for key in (first, second):
        run, case, rep = key.split(inject.KEY_SEPARATOR)
        assert run.startswith(harness._RUN_ID_PREFIX)
        assert case == harness._EVAL_CASE_FALLBACK
        assert rep == harness._EVAL_REPETITION_FALLBACK
    assert stub_gateway.submissions[0]["messageId"] == first


def test_a_pinned_conversation_id_never_reaches_the_inject_key(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGENT_CONVERSATION_ID pins the api path's conversation. On this path the
    run id is the first segment of the message id the door dedupes on, so a
    pinned one would have the door answer a rerun with a previous invocation's
    task and replay its terminal as this run's. It is minted fresh, always."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")
    monkeypatch.setattr(harness, "_mint_run_id", REAL_MINT_RUN_ID)
    monkeypatch.setenv("AGENT_CONVERSATION_ID", "pinned-from-yesterday")

    result = KubeAgentsHarness().run("go")

    run = str(result.metadata["run_id"])
    assert run != "pinned-from-yesterday"
    assert run.startswith(harness._RUN_ID_PREFIX)
    assert stub_gateway.submissions[0]["messageId"].startswith(run + inject.KEY_SEPARATOR)


def test_the_preflight_read_precedes_the_post(stub_gateway: _StubGatewayServer) -> None:
    """The budget floor and the inject-only fact are learned from a read of the
    fresh conversation before anything is started -- a pure read, so asking
    mints nothing."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")

    KubeAgentsHarness().run("go")

    assert stub_gateway.calls[:2] == ["preflight", "submit"]
    preflight = stub_gateway.polls[0]
    assert preflight["probe"] and not preflight["task"]


# --------------------------------------------------------------------------
# Classification: infrastructure versus graded.


def test_a_task_nothing_executed_is_infrastructure(stub_gateway: _StubGatewayServer) -> None:
    """The classification the harness makes from the read rather than from a
    clock of its own: the record holds the task, its stream has no event at
    all, and it is older than the gateway's grace. Nobody took it -- an
    install with no executor -- and that is not the agent's answer. Nothing
    is sent to find out: no message. The cancel comes after the read, names
    the task, and changes nothing about the verdict: the submission is still
    on the bus for a bridge that binds later, and the cancel bounds that run."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = [""]
    stub_gateway.probe_age_seconds = GRACE_SECONDS + 1

    started = time.monotonic()
    result = KubeAgentsHarness().run("nobody is listening")
    elapsed = time.monotonic() - started

    assert infra(result)
    assert "no executor took task" in result.errors[0]
    assert "a cancel naming the task was published" in result.errors[0]
    # Never the agent's answer: AgentResult.errored would put this text in
    # front of the judge as the reply.
    assert result.output == ""
    # The verdict came from the first read that showed it, not from a
    # deadline thirty minutes out -- and the cancel did not wait on a settle.
    assert elapsed < 10, elapsed
    assert len(stub_gateway.submissions) == 1
    assert [c["taskId"] for c in stub_gateway.cancels] == [stub_gateway.task_id]
    assert stub_gateway.calls.index("probe") < stub_gateway.calls.index("cancel")


def test_a_never_started_task_already_detached_is_not_cancelled_again(
    stub_gateway: _StubGatewayServer,
) -> None:
    """The record already carries a published cancel (detached): no second one
    is sent, and the record says a stop was pending rather than claiming a
    cancel this run never published."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = [""]
    stub_gateway.probe_age_seconds = GRACE_SECONDS + 1
    stub_gateway.probe_detached = True

    result = KubeAgentsHarness().run("nobody is listening")

    assert infra(result)
    assert "no executor took task" in result.errors[0]
    assert "a stop was already pending" in result.errors[0]
    assert "was published" not in result.errors[0]
    assert not stub_gateway.cancels


def test_a_cancel_the_door_did_not_publish_is_not_recorded_as_published(
    stub_gateway: _StubGatewayServer,
) -> None:
    """A 200 from the cancel route says the door answered, not that a cancel
    reached the bus: the gateway refuses a task the conversation never held
    and one whose publish failed, and posts a line saying so. Recording those
    as published would tell a reader a stray run was bounded while it runs."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = [""]
    stub_gateway.probe_age_seconds = GRACE_SECONDS + 1
    stub_gateway.cancel_published = False

    result = KubeAgentsHarness().run("nobody is listening")

    assert infra(result)
    assert "no executor took task" in result.errors[0]
    assert "could not be sent" in result.errors[0]
    assert "was published" not in result.errors[0]
    # It was still attempted: not sending one would leave the submission on
    # the bus with nothing having tried to bound it.
    assert [c["taskId"] for c in stub_gateway.cancels] == [stub_gateway.task_id]


def test_a_transport_that_dies_after_the_accept_still_cancels_the_task(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exchange gives up when the gateway cannot be read at all, and by
    then the task is running on the bridge. Left alone it holds a concurrency
    slot until the bridge's own deadline and the units behind it queue, so the
    harness cancels on the way out -- best effort, over the transport that
    just failed."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)
    # Outside the retry set, so the first read after the accept raises at
    # once. Bounded to one failure so the preflight read, which precedes the
    # POST, still succeeds and there is a task to abandon.
    stub_gateway.poll_status = 500
    stub_gateway.poll_failures = 1

    result = KubeAgentsHarness().run("take your time")

    assert infra(result)
    assert len(stub_gateway.submissions) == 1
    assert [c["taskId"] for c in stub_gateway.cancels] == [stub_gateway.task_id]
    assert "was published" in result.errors[0]


def test_a_task_inside_the_grace_is_still_waited_for(stub_gateway: _StubGatewayServer) -> None:
    """The same read inside the grace is not a verdict: a first event that is
    merely late may still arrive, so the harness keeps polling."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = ["", "", "working"]
    stub_gateway.probe_age_seconds = 0

    def finish() -> None:
        time.sleep(0.4)
        stub_gateway.entries = completed_transcript(stub_gateway.task_id, "late but fine")

    threading.Thread(target=finish, daemon=True).start()
    result = KubeAgentsHarness().run("patience")

    assert result.output == "late but fine"
    assert not infra(result)


def test_a_failed_task_is_graded_rather_than_classified(
    stub_gateway: _StubGatewayServer,
) -> None:
    """A task an executor took and ended `failed` for the persona's own reason
    is the agent's outcome. It stays in front of the judge with the terminal
    and its reason on errors."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_POST, text="❌ failed: the cluster is unreachable", messageId=RESULT_ID),
        entry(4, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="failed",
              source=inject.TERMINAL_SOURCE_EXECUTOR,
              reason="reason: hermes-exited-nonzero - exit status 1"),
    ]

    result = KubeAgentsHarness().run("break")

    assert result.metadata["terminal_state"] == "failed"
    assert result.metadata["reason_token"] == "hermes-exited-nonzero"
    assert "the cluster is unreachable" in result.output
    assert any("ended failed (reason: hermes-exited-nonzero)" in e for e in result.errors)
    assert not infra(result)


@pytest.mark.parametrize("token", sorted(inject.INFRASTRUCTURE_REASONS))
def test_an_executors_own_failure_is_infrastructure(
    stub_gateway: _StubGatewayServer, token: str
) -> None:
    """A failed terminal is not always the persona's failure. The bridge's and
    the worker adapter's own reasons say the executor broke around the task,
    which is the same class as an exhausted transport retry."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_POST, text=f"❌ failed: reason: {token}", messageId=RESULT_ID),
        entry(4, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="failed",
              source=inject.TERMINAL_SOURCE_EXECUTOR, reason=f"reason: {token} - detail here"),
    ]

    result = KubeAgentsHarness().run("go")

    assert infra(result)
    assert token in result.errors[0]
    assert result.output == ""


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("rejected", "reason: no-text-parts - the submission message carries nothing"),
        ("canceled", "reason: canceled-before-start"),
    ],
    ids=["rejected", "canceled-before-start"],
)
def test_a_task_that_never_ran_is_infrastructure(
    stub_gateway: _StubGatewayServer, state: str, reason: str
) -> None:
    """A rejected submission and a task cancelled out of the bridge's queue
    both ended before anything ran. Neither is an answer."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state=state,
              source=inject.TERMINAL_SOURCE_EXECUTOR, reason=reason),
    ]

    result = KubeAgentsHarness().run("go")

    assert infra(result)
    assert result.output == ""


def test_an_unknown_reason_is_graded(stub_gateway: _StubGatewayServer) -> None:
    """A reason this side does not know, or no reason at all, is the persona's
    failure until an executor definition says otherwise."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="failed",
              source=inject.TERMINAL_SOURCE_EXECUTOR, reason="something new went wrong"),
    ]

    result = KubeAgentsHarness().run("go")

    assert not infra(result)
    assert result.metadata["reason_token"] is None
    assert any("ended failed (reason: no reason given)" in e for e in result.errors)


def test_a_submission_the_gateway_refuses_is_infrastructure(
    stub_gateway: _StubGatewayServer,
) -> None:
    """No task means no agent saw the prompt. The commonest cause is an author
    the principal map does not carry, which is a misconfigured install rather
    than a bad answer -- and it must not be retried, because the answer cannot
    change."""
    stub_gateway.accept_submissions = False
    stub_gateway.refusal_code = inject.REFUSAL_UNVERIFIED_AUTHOR
    stub_gateway.refusal_note = "an author the principal map does not know"

    result = KubeAgentsHarness().run("who am I?")

    assert infra(result)
    assert "the gateway refused the injection" in result.errors[0]
    assert "principal map" in result.errors[0]
    assert result.output == ""
    assert len(stub_gateway.submissions) == 1


def test_a_gateway_declared_terminal_is_infrastructure(
    stub_gateway: _StubGatewayServer,
) -> None:
    """"The task failed" and "the gateway could not put the task on the bus"
    are the same state and opposite meanings. Grading the second scores a bus
    outage against the agent."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_EDIT, text="❌ could not reach the bus; try again",
              messageId=PLACEHOLDER_ID),
        entry(4, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="failed",
              source=inject.TERMINAL_SOURCE_GATEWAY),
    ]

    result = KubeAgentsHarness().run("go")

    assert infra(result)
    assert "failed to publish the submission" in result.errors[0]
    assert "never reached the bus" in result.errors[0]
    assert result.output == ""


@pytest.mark.parametrize(
    ("refusal", "phrase"),
    [
        (inject.REFUSAL_UNVERIFIED_AUTHOR, "refused the injection"),
        (inject.REFUSAL_PUBLISH_FAILED, "failed to publish the submission"),
        (inject.REFUSAL_NO_TASK, "answered the turn without starting a task"),
        (inject.REFUSAL_NO_ANSWER, "did not say what it did with the prompt"),
        ("", "started no task for the prompt"),
    ],
)
def test_each_refusal_says_which_thing_is_broken(
    stub_gateway: _StubGatewayServer, refusal: str, phrase: str
) -> None:
    """Every refusal is infrastructure, and each names a different broken
    thing: an author the map does not carry, a bus the gateway could not
    reach, a message that landed as a steer, a gateway that said nothing. The
    code is what the harness branches on -- the note beside it is the
    gateway's prose and is free to change -- and an unknown code still reads
    as "nothing ran". None of them is retried: the door answers a repeat of
    the same message id with the same refusal."""
    stub_gateway.accept_submissions = False
    stub_gateway.refusal_code = refusal

    result = KubeAgentsHarness().run("go")

    assert infra(result)
    assert phrase in result.errors[0]
    assert result.output == ""
    assert len(stub_gateway.submissions) == 1
    assert not stub_gateway.cancels


# --------------------------------------------------------------------------
# The transport retry and the door's dedupe.


def test_an_unreachable_gateway_is_infrastructure_after_its_retries(
    stub_gateway: _StubGatewayServer,
) -> None:
    """A 503 clears on its own, so it is retried; exhausting the retries is
    the run class, not an answer."""
    stub_gateway.submit_status = 503

    result = KubeAgentsHarness().run("hello")

    assert infra(result)
    assert len(stub_gateway.submissions) == harness._MAX_TRANSPORT_FAILURES


@pytest.mark.parametrize("status", sorted(inject.RETRYABLE_STATUSES))
def test_a_retried_post_carries_the_same_body_and_is_deduped(
    stub_gateway: _StubGatewayServer, status: int
) -> None:
    """The retry is the same message: same body, same message id. The door
    answers a repeat with the task the first POST started, so a retry that
    reached the gateway after all cannot become a steer on the running
    task."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")
    stub_gateway.submit_status = status
    stub_gateway.submit_failures = 1
    # The first POST was accepted at the door before the reply was lost.
    stub_gateway.accepted_ids.append(CONVERSATION)

    result = KubeAgentsHarness().run("hello")

    assert result.output == "done"
    assert len(stub_gateway.submissions) == 2
    assert stub_gateway.submissions[0] == stub_gateway.submissions[1]
    assert stub_gateway.submissions[1]["messageId"] == CONVERSATION


def test_a_transport_failure_after_the_accept_rejoins_the_same_wait(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll lost mid-task -- the port-forward dropping, a 5xx from the door
    -- is retried without a second POST, the fold is rebuilt from the start
    of the conversation, and the retry rejoins the budget already running
    rather than starting a new one. A budget that restarted would triple a
    30-minute run's ceiling on two dropped polls."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id, "partial findings")
    stub_gateway.poll_status = 503
    stub_gateway.poll_failures = 2
    # A second into the task, so the polls before and after the failures
    # visibly ask the gateway to wait for different remainders.
    stub_gateway.poll_fail_after_seconds = 1.0
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "3")

    result = KubeAgentsHarness().run("take your time")

    assert stub_gateway.failed_polls == 2
    assert len(stub_gateway.submissions) == 1, "the retry re-sent the prompt"
    assert result.output == "partial findings"
    assert any("did not reach a terminal state" in e for e in result.errors)
    assert not infra(result)
    assert [c["taskId"] for c in stub_gateway.cancels] == [stub_gateway.task_id]
    failed_at = max(i for i, p in enumerate(stub_gateway.polls) if p["failed"])
    before = stub_gateway.polls[failed_at - 2]
    resumed = stub_gateway.polls[failed_at + 1]
    # Replayed from the start, so the deliverable posted before the drop is
    # in the fold again.
    assert resumed["after"] == 0
    # And on the original clock: the poll before the drop asked for the
    # whole remainder of a fresh budget, the one after it for what was left.
    assert before["wait"] == 2
    assert resumed["wait"] <= 1, f"the budget restarted: {resumed}"


def test_a_status_outside_the_retry_set_is_not_retried(stub_gateway: _StubGatewayServer) -> None:
    """A 500 is a bug at the door and a 400 is this request being wrong;
    re-sending either only repeats it."""
    stub_gateway.submit_status = 500

    result = KubeAgentsHarness().run("hello")

    assert infra(result)
    assert len(stub_gateway.submissions) == 1


# --------------------------------------------------------------------------
# The deadline: one read, then the cancel.


def test_a_working_task_at_the_deadline_is_a_graded_timeout(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task the executor is working on at the budget is graded on what it
    produced, with the overrun on errors -- and the executor is asked to
    stop, after the read and never before it."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id, "partial findings")
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "2")

    result = KubeAgentsHarness().run("take your time")

    assert result.metadata["terminal_state"] is None
    assert result.output == "partial findings"
    assert any("did not reach a terminal state" in e for e in result.errors)
    assert not infra(result)
    assert [c["taskId"] for c in stub_gateway.cancels] == [stub_gateway.task_id]
    assert stub_gateway.calls.index("probe") < stub_gateway.calls.index("cancel")


def test_a_queued_task_at_the_deadline_is_infrastructure(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge publishes submitted when it queues a task behind its
    concurrency cap and working only when it spawns. A task that only ever
    reached submitted by the budget never ran: infrastructure, and the
    cancel asks the bridge to drop it from the queue."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = ["submitted"]
    stub_gateway.on_cancel = [
        entry(3, inject.ENTRY_POST, text="🛑 cancel sent", messageId="inj-5"),
        entry(4, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="canceled",
              source=inject.TERMINAL_SOURCE_EXECUTOR, reason="reason: canceled-before-start"),
    ]
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "2")

    result = KubeAgentsHarness().run("wait in line")

    assert infra(result)
    assert "sat queued" in result.errors[0]
    assert "BRIDGE_CONCURRENCY" in result.errors[0]
    assert result.output == ""
    assert stub_gateway.cancels
    assert stub_gateway.calls.index("probe") < stub_gateway.calls.index("cancel")


def test_a_queued_task_already_detached_is_infrastructure_not_graded(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task that only ever reached submitted never ran, whether or not
    someone else's cancel is already pending on its record. Graded, its
    record would carry a lone submitted entry and be blocked at rung 3 as a
    run that never happened -- which is the truth, said as infrastructure
    rather than as a verdict. No second cancel is sent."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = ["submitted"]
    stub_gateway.probe_detached = True
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "2")

    result = KubeAgentsHarness().run("wait in line")

    assert infra(result)
    assert "sat queued" in result.errors[0]
    assert "a stop was already pending" in result.errors[0]
    assert not stub_gateway.cancels


def test_the_cancel_goes_through_the_route_not_the_stop_text(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway's stop phrase is an affordance for a human. A program
    reaching a control path through a phrase list breaks when the list
    changes, and cannot be told apart from a user asking the agent to stop
    something in the world."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "2")

    KubeAgentsHarness().run("take your time")

    assert stub_gateway.cancels, "nothing reached the cancel route"
    # The cancel names its author, because it is verified like any other
    # message on the conversation, and its message id joins to the ask's.
    assert stub_gateway.cancels[0]["author"] == inject.DEFAULT_AUTHOR
    assert stub_gateway.cancels[0]["messageId"] == CONVERSATION + inject.CANCEL_MESSAGE_ID_SUFFIX
    for submission in stub_gateway.submissions:
        assert submission.get("text", "").strip().lower() != "stop"


@pytest.mark.parametrize(
    ("active", "error"),
    [
        (True, "the stream is unreachable"),
        (False, ""),
    ],
    ids=["could-not-look", "released-with-no-terminal"],
)
def test_an_unclassifiable_deadline_is_infrastructure(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch, active: bool, error: str
) -> None:
    """A deadline the read cannot classify -- the gateway could not look, or
    the record no longer holds the task and this side never saw a terminal --
    says nothing about whether an executor saw the prompt, so nothing is
    graded. The cancel still goes out, naming the task the POST answered
    with: the gateway publishes it whether or not its record holds the task."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.probe_active = active
    stub_gateway.probe_error = error
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "2")

    result = KubeAgentsHarness().run("take your time")

    assert infra(result)
    assert "could not be classified" in result.errors[0]
    if error:
        assert error in result.errors[0]
    else:
        assert "no active task" in result.errors[0]
    assert result.output == ""
    assert [c["taskId"] for c in stub_gateway.cancels] == [stub_gateway.task_id]


def test_a_transient_settle_read_does_not_relabel_a_graded_deadline(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline read said working, which is the classification. The settle
    read after the cancel is adopted only if it brought the executor's
    terminal; a transient there must not turn a graded timeout into an
    infrastructure failure."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id, "partial findings")
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "2")

    real_cancel = inject.InjectTask.cancel

    def failing_after_cancel(self: inject.InjectTask, *args: Any, **kwargs: Any) -> Any:
        stub_gateway.probe_error = "the stream is unreachable"
        return real_cancel(self, *args, **kwargs)

    monkeypatch.setattr(inject.InjectTask, "cancel", failing_after_cancel)

    result = KubeAgentsHarness().run("take your time")

    assert stub_gateway.cancels
    assert result.output == "partial findings"
    assert any("did not reach a terminal state" in e for e in result.errors)
    assert not infra(result)


def test_a_confirmed_cancel_keeps_the_deadline_on_record(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the executor confirms the cancel, the record says both how the task
    ended (canceled, by request) and why (the harness's deadline) -- and the
    bridge's canceled reason after our own cancel is the graded timeout, not
    infrastructure."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id, "partial findings")
    stub_gateway.on_cancel = [
        entry(5, inject.ENTRY_POST, text="🛑 cancel sent", messageId="inj-5"),
        entry(6, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="canceled",
              source=inject.TERMINAL_SOURCE_EXECUTOR, reason="reason: canceled-by-request"),
    ]
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "2")

    result = KubeAgentsHarness().run("take your time")

    # The deliverable is what the task produced inside its budget; the
    # gateway's cancel acknowledgement, posted after it, is not the answer.
    assert result.output == "partial findings"
    assert result.metadata["terminal_state"] == "canceled"
    assert result.metadata["reason_token"] == "canceled-by-request"
    assert any("did not reach a terminal state" in e for e in result.errors)
    assert any("ended canceled" in e for e in result.errors)
    assert not infra(result)
    assert status_entries(result)[-1] == ("canceled", True)


def test_a_detached_task_at_the_deadline_is_not_cancelled_twice(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record the gateway has already published a cancel for is graded like
    a working one, and no second cancel is sent."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id, "partial findings")
    stub_gateway.probe_detached = True
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "2")

    result = KubeAgentsHarness().run("take your time")

    assert result.output == "partial findings"
    assert any("a stop was already pending" in e for e in result.errors)
    assert not stub_gateway.cancels


def test_a_finished_read_waits_for_the_relay_then_adopts_the_stream(
    stub_gateway: _StubGatewayServer,
) -> None:
    """A read that finds the task terminal on the stream does not heal (the
    relay may be a moment from posting the deliverable). The harness waits
    briefly for the relay's terminal and, if none comes, adopts the stream's
    state as the executor's word -- graded, never cancelled."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id, "the answer")
    stub_gateway.executor_states = ["completed"]
    stub_gateway.probe_final = True
    stub_gateway.probe_terminal_source = inject.TERMINAL_SOURCE_EXECUTOR

    result = KubeAgentsHarness().run("quick one")

    assert result.output == "the answer"
    assert result.metadata["terminal_state"] == "completed"
    assert result.metadata["terminal_source"] == inject.TERMINAL_SOURCE_EXECUTOR
    assert result.metadata["outcome"] == inject.OUTCOME_STREAM_TERMINAL
    assert result.errors == []
    assert not stub_gateway.cancels


def test_a_record_the_relay_left_active_on_a_finished_stream_is_graded_from_the_fold(
    stub_gateway: _StubGatewayServer,
) -> None:
    """The relay acks a terminal before it clears the record, so a restart or
    a failed record write leaves an active record on a finished task, and on
    a key never reused no heal arrives. The conversation received nothing --
    the pod that restarted took the transcript with it -- so the answer is
    the result text the read's fold carries. Graded like a finished run, with
    the executor's terminal on the record; no cancel, nothing is running."""
    # The transcript after a restart: the in-memory door came back empty.
    stub_gateway.entries = []
    stub_gateway.executor_states = ["completed"]
    stub_gateway.probe_final = True
    stub_gateway.probe_terminal_source = inject.TERMINAL_SOURCE_EXECUTOR
    stub_gateway.probe_result = "the PodDisruptionBudget blocks the drain"

    result = KubeAgentsHarness().run("why is the drain stuck")

    assert not infra(result)
    assert result.output == "the PodDisruptionBudget blocks the drain"
    assert result.metadata["final_message"] == result.output
    assert result.metadata["outcome"] == inject.OUTCOME_STREAM_TERMINAL
    assert result.metadata["terminal_state"] == "completed"
    assert result.errors == []
    assert status_entries(result)[-1] == ("completed", True)
    assert not stub_gateway.cancels


def test_a_failed_terminal_on_the_fold_carries_its_reason(
    stub_gateway: _StubGatewayServer,
) -> None:
    """The same lost record write on a task that failed: the fold's reason is
    the executor's, so the persona's failure is graded with its token on the
    record, exactly as it would be had the relay posted it."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = ["failed"]
    stub_gateway.probe_final = True
    stub_gateway.probe_terminal_source = inject.TERMINAL_SOURCE_EXECUTOR
    stub_gateway.probe_reason = "reason: hermes-exited-nonzero - exit status 1"

    result = KubeAgentsHarness().run("go")

    assert not infra(result)
    assert result.metadata["terminal_state"] == "failed"
    assert result.metadata["reason_token"] == inject.REASON_HERMES_EXITED_NONZERO
    assert any("ended failed" in e for e in result.errors)
    assert not stub_gateway.cancels


def test_an_adopted_fold_grades_the_result_text_over_the_placeholder(
    stub_gateway: _StubGatewayServer,
) -> None:
    """On the lost-record-write path the relay posted nothing but the
    gateway's own "submitted" placeholder, and nothing ever edited it, so it
    survives the rewrite filter and would be graded as the agent's answer.
    The result artifact the read carries off the stream is the answer."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = [inject.STATE_COMPLETED]
    stub_gateway.probe_final = True
    stub_gateway.probe_terminal_source = inject.TERMINAL_SOURCE_EXECUTOR
    stub_gateway.probe_result = "the PodDisruptionBudget allows zero disruptions"

    result = KubeAgentsHarness().run("why is the drain stuck?")

    assert not infra(result)
    assert result.output == "the PodDisruptionBudget allows zero disruptions"
    assert PLACEHOLDER not in result.output
    assert result.metadata["final_message"] == result.output


def test_a_supervisor_terminal_on_the_fold_is_infrastructure(
    stub_gateway: _StubGatewayServer,
) -> None:
    """A terminal on the fold that the gateway attributes to itself -- the
    supervisor's word about an executor that died or never ran -- is not an
    executor's answer. Only an executor's terminal on the fold grades; this
    one is infrastructure, and nothing is cancelled: nothing is running."""
    stub_gateway.entries = running_transcript(stub_gateway.task_id)[:2]
    stub_gateway.executor_states = ["failed"]
    stub_gateway.probe_final = True
    stub_gateway.probe_terminal_source = inject.TERMINAL_SOURCE_SUPERVISOR
    stub_gateway.probe_reason = "reason: worker-evicted"

    result = KubeAgentsHarness().run("go")

    assert infra(result)
    assert "ended by the gateway rather than by an executor" in result.errors[0]
    assert "the supervisor ended it" in result.errors[0]
    assert "worker-evicted" in result.errors[0]
    assert result.output == ""
    assert not stub_gateway.cancels


# --------------------------------------------------------------------------
# The budget floor.


def test_a_budget_below_the_floor_is_refused_before_anything_starts(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget inside the gateway's grace could expire while a task no
    executor has touched is still legitimately pre-first-event, and the read
    at the deadline could not tell a slow install from one with no executor.
    So it is refused, from the preflight read, with nothing submitted."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")
    monkeypatch.setattr(inject, "GRACE_MARGIN_SECONDS", 120.0)
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "60")

    result = KubeAgentsHarness().run("go")

    assert result.errors and "AGENT_INJECT_TIMEOUT" in result.errors[0]
    assert "below the floor" in result.errors[0]
    assert not stub_gateway.submissions
    assert stub_gateway.calls == ["preflight"]


def test_the_budget_floor_is_the_grace_plus_a_real_margin() -> None:
    """The floor at its real size, pinned here because the fixture shrinks the
    margin for every other test."""
    grace = 600.0  # A2A_FIRST_EVENT_GRACE's default, ten minutes.
    assert inject.GRACE_MARGIN_SECONDS >= 60.0
    assert inject.budget_floor(grace) == grace + inject.GRACE_MARGIN_SECONDS
    # The default budget clears the default grace's floor, and the api
    # transport's per-request default would not have.
    assert inject.check_budget(float(harness._INJECT_DEFAULT_TIMEOUT), grace) == 1800.0
    with pytest.raises(inject.BudgetBelowFloor):
        inject.check_budget(600.0, grace)
    # An install that reports no grace (an older gateway) still gets a floor
    # of the margin alone rather than nothing.
    assert inject.check_budget(1800.0, 0.0) == 1800.0


# --------------------------------------------------------------------------
# The token.


def test_every_request_carries_the_bearer_token(stub_gateway: _StubGatewayServer) -> None:
    """The door authenticates every route, so the transport presents the
    token on every route -- a GET that did not would read every reply on
    every conversation without one."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")

    KubeAgentsHarness().run("check")

    assert stub_gateway.seen_authorization, "the stub saw no requests at all"
    for presented in stub_gateway.seen_authorization:
        assert presented == inject.BEARER_PREFIX + TOKEN, presented


def test_a_run_without_the_token_is_infrastructure(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with no token never reaches the agent, so it is the install's
    configuration rather than the agent's answer -- and the message has to
    say where the token comes from, because that is the whole fix."""
    monkeypatch.delenv("AGENT_INJECT_TOKEN", raising=False)

    result = KubeAgentsHarness().run("hello")

    assert infra(result)
    assert "AGENT_INJECT_TOKEN" in result.errors[0]
    assert "a2a-inject" in result.errors[0]
    assert result.output == ""
    assert not stub_gateway.submissions


def test_a_refused_token_is_not_retried(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 is this request being wrong, and sending it again cannot change
    the answer -- unlike the 503 that clears on its own."""
    monkeypatch.setenv("AGENT_INJECT_TOKEN", "not-the-token")

    result = KubeAgentsHarness().run("hello")

    assert infra(result)
    assert "refused the bearer token" in result.errors[0]
    assert len(stub_gateway.seen_authorization) == 1, "a refused token was retried"


# --------------------------------------------------------------------------
# Unit tests on the fold and the reason parser.


def test_an_unknown_transport_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TRANSPORT", "carrier-pigeon")
    result = KubeAgentsHarness().run("hi")
    assert "AGENT_TRANSPORT" in result.errors[0]
    assert "carrier-pigeon" in result.errors[0]


def test_the_fold_ignores_another_task_s_terminal() -> None:
    """A conversation carries more than one task once the case runner sends a
    follow-up, and an earlier task's terminal must not end this one's wait."""
    fold = inject.Fold("task-mine")
    fold.apply(entry(1, inject.ENTRY_TERMINAL, taskId="task-theirs", state="completed"))
    assert not fold.final
    fold.apply(entry(2, inject.ENTRY_TERMINAL, taskId="task-mine", state="failed"))
    assert fold.final
    assert fold.terminal == "failed"


def test_the_fold_counts_what_it_cannot_read() -> None:
    """A malformed entry is skipped and counted rather than failing the fold,
    the way the bus library's replay skips a poison write."""
    fold = inject.Fold("task-1")
    for bad in ["not a dict", {"kind": "post"}, {"seq": 1}, entry(2, "unheard-of")]:
        fold.apply(bad)
    assert fold.malformed == 4
    assert fold.entries == [] or all(e["kind"] != inject.ENTRY_POST for e in fold.entries)


def test_a_chunked_answer_is_reassembled_whole(stub_gateway: _StubGatewayServer) -> None:
    """The gateway splits any post over its chunk cap into separate posts, so
    an agent report of any real length arrives as several. Grading the last
    one alone fails a phrase check on a report that named the cause in its
    first paragraph -- which is most of them."""
    head = "Root cause: OOMKilled on payments-api.\n"
    body = "x" * 4000
    tail = "\nRemediation: raise the memory limit."
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_EDIT, text="⚙️ **working**", messageId=PLACEHOLDER_ID),
        entry(4, inject.ENTRY_POST, text=head, messageId="inj-10"),
        entry(5, inject.ENTRY_POST, text=body, messageId="inj-11"),
        entry(6, inject.ENTRY_POST, text=tail, messageId="inj-12"),
        entry(7, inject.ENTRY_EDIT, text="✅ **completed**", messageId=PLACEHOLDER_ID),
        entry(8, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="completed",
              source=inject.TERMINAL_SOURCE_EXECUTOR),
    ]

    result = KubeAgentsHarness().run("why did it crash?")

    # Reassembled exactly, with nothing inserted between the chunks: the
    # gateway cuts mid-text where it must, so a separator could land inside
    # the phrase a verifier is looking for.
    assert result.output == head + body + tail


def test_an_earlier_task_s_posts_are_not_this_task_s_answer() -> None:
    """A conversation carries every task's posts once the case runner sends a
    follow-up. Folding the previous task's answer into this one's would grade
    a stale reply."""
    fold = inject.Fold("task-second")
    fold.apply(entry(1, inject.ENTRY_TASK, taskId="task-first"))
    fold.apply(entry(2, inject.ENTRY_POST, text="the first answer", messageId="inj-1"))
    fold.apply(entry(3, inject.ENTRY_TERMINAL, taskId="task-first", state="completed"))
    fold.apply(entry(4, inject.ENTRY_TASK, taskId="task-second"))
    fold.apply(entry(5, inject.ENTRY_POST, text="the second answer", messageId="inj-2"))

    assert fold.deliverable == "the second answer"


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("reason: hermes-exited-nonzero - exit status 1; stderr tail: boom", "hermes-exited-nonzero"),
        ("reason: bridge-shutdown - the bridge was terminated", "bridge-shutdown"),
        ("reason: bus-publish-failed at working", "bus-publish-failed"),
        ("reason: canceled-before-start", "canceled-before-start"),
        ("  reason: worker-evicted - SIGTERM  ", "worker-evicted"),
        ("the task failed", ""),
        ("", ""),
        ("reason:", ""),
    ],
)
def test_the_reason_token_is_the_word_after_the_prefix(text: str, token: str) -> None:
    """The bridge and the worker adapter write ``reason: <token>[ - detail]``;
    the token is what is classified, and a message without the prefix has no
    token."""
    assert inject.parse_reason(text) == token


@pytest.mark.parametrize(
    ("state", "reason", "infrastructure"),
    [
        *[("failed", f"reason: {t} - detail", True) for t in sorted(inject.INFRASTRUCTURE_REASONS)],
        *[("failed", f"reason: {t} - detail", False) for t in sorted(inject.PERSONA_REASONS)],
        ("failed", "reason: never-heard-of-it", False),
        ("failed", "", False),
        ("rejected", "reason: no-text-parts - nothing to run", True),
        ("canceled", "reason: canceled-before-start", True),
        ("canceled", "reason: canceled-by-request", False),
        ("completed", "", False),
    ],
)
def test_each_reason_class_is_named(state: str, reason: str, infrastructure: bool) -> None:
    """Every executor reason lands in its class: the bridge's and the worker
    adapter's own as infrastructure, the persona's and the unknown as graded,
    rejected and canceled-before-start as infrastructure, canceled-by-request
    as the graded timeout."""
    fold = inject.Fold("task-1")
    fold.apply(entry(1, inject.ENTRY_TERMINAL, taskId="task-1", state=state,
                     source=inject.TERMINAL_SOURCE_EXECUTOR, reason=reason))
    assert bool(fold.infrastructure_terminal) is infrastructure
    assert fold.reason_token == inject.parse_reason(reason)


def test_the_infrastructure_reasons_are_the_executors_own() -> None:
    """The set is the bridge's five plus the worker adapter's two, named
    against their definitions; the persona's are not in it."""
    assert inject.INFRASTRUCTURE_REASONS == {
        "bridge-shutdown",
        "bridge-queue-overflow",
        "bus-publish-failed",
        "spawn-failed",
        "bridge-died-without-terminal-event",
        "worker-evicted",
        "bus-subscribe-failed",
    }
    assert not inject.INFRASTRUCTURE_REASONS & inject.PERSONA_REASONS


def test_the_fold_records_the_lifecycle_once_per_state() -> None:
    """Many reads see the same state; the record carries it once, in order,
    and the terminal closes it as the final entry."""
    fold = inject.Fold("task-1")
    for state in ["submitted", "submitted", "working", "working"]:
        fold.note_executor_state(state)
    fold.mark_terminal("completed", inject.TERMINAL_SOURCE_EXECUTOR, "")
    fold.mark_terminal("failed", inject.TERMINAL_SOURCE_EXECUTOR, "reason: too-late")
    assert fold.executor_states == ["submitted", "working", "completed"]
    assert [(e["args"]["state"], e["args"]["final"]) for e in fold.trajectory] == [
        ("submitted", False),
        ("working", False),
        ("completed", True),
    ]
    assert fold.terminal == "completed"
    assert not fold.queued_only
    queued = inject.Fold("task-2")
    queued.note_executor_state("submitted")
    assert queued.queued_only
