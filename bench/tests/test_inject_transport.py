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

A local HTTP stub stands in for the A2A gateway's inject backend, serving the
two endpoints the real one serves and scripted with the transcript a given
task produced. ``AGENT_INJECT_URL`` points the harness at it, so nothing here
spawns ``kubectl``. This exercises the full submit -> poll -> fold ->
AgentResult path the eval harness consumes.

The stub's replies are shaped from what the gateway actually posts
(``a2a/gateway/relay.go``): a placeholder that gets edited as the rolling
progress line, the deliverable as its own post, and a terminal entry after
both.
"""

from __future__ import annotations

import json
import threading
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
        entry(6, inject.ENTRY_TERMINAL, taskId=task_id, state="completed"),
    ]


class _StubGatewayHandler(BaseHTTPRequestHandler):
    """The inject backend's two endpoints, scripted per test."""

    server: _StubGatewayServer

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        self.server.submissions.append(request)
        if self.server.submit_status is not None:
            self._respond(self.server.submit_status, {"error": "no"})
            return
        conversation = "inject:" + str(request.get("conversation") or "")
        # A stop is an ordinary message; the gateway answers it without
        # starting a task, and so does this.
        if request.get("text") == inject.STOP_TEXT:
            self.server.stops.append(conversation)
            self._respond(200, {"conversation": conversation, "accepted": False, "note": "stopped"})
            return
        if not self.server.accept_submissions:
            self._respond(
                200,
                {
                    "conversation": conversation,
                    "accepted": False,
                    "note": self.server.refusal_note,
                    "entries": self.server.refusal_entries,
                },
            )
            return
        self._respond(
            200,
            {
                "conversation": conversation,
                "taskId": self.server.task_id,
                "accepted": True,
                "messageId": "inj-msg-1",
                "entries": [],
            },
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        after = int(query.get("after", ["0"])[0])
        self.server.polls.append({"path": parsed.path, "after": after, "task": query.get("task")})
        if self.server.poll_status is not None:
            self.send_error(self.server.poll_status)
            return
        fresh = [e for e in self.server.entries if e["seq"] > after]
        # The gateway serves at most what it has; a page cap is what makes the
        # `after` contract worth testing at all.
        page = fresh[: self.server.page_size] if self.server.page_size else fresh
        last_seq = page[-1]["seq"] if page else (self.server.entries[-1]["seq"] if self.server.entries else 0)
        body: dict[str, Any] = {
            "conversation": "inject:" + self.server.conversation,
            "entries": page,
            "lastSeq": last_seq,
        }
        if self.server.terminal_out_of_band and not fresh:
            body["terminal"] = self.server.terminal_out_of_band
        self._respond(200, body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep pytest output clean


class _StubGatewayServer(ThreadingHTTPServer):
    task_id: str = "task-abc123"
    conversation: str = "devops-bench-test"
    entries: list[dict[str, Any]]
    submissions: list[dict[str, Any]]
    polls: list[dict[str, Any]]
    stops: list[str]
    accept_submissions: bool = True
    refusal_note: str = "the gateway answered without starting a task"
    refusal_entries: list[dict[str, Any]]
    # Non-None makes every POST (or GET) answer with that status instead.
    submit_status: int | None = None
    poll_status: int | None = None
    # Serve at most this many entries per GET; 0 means all of them.
    page_size: int = 0
    # A terminal the gateway reports out of band once the entries are drained,
    # which is how a late reader learns an answer it missed.
    terminal_out_of_band: str = ""


@pytest.fixture
def stub_gateway(monkeypatch: pytest.MonkeyPatch) -> Generator[_StubGatewayServer, None, None]:
    server = _StubGatewayServer(("127.0.0.1", 0), _StubGatewayHandler)
    server.entries = []
    server.submissions = []
    server.polls = []
    server.stops = []
    server.refusal_entries = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    monkeypatch.setenv("AGENT_INJECT_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AGENT_INJECT_CONVERSATION", server.conversation)
    # The poll pause is dead time in a test whose stub answers instantly.
    monkeypatch.setattr(inject, "POLL_PAUSE_SECONDS", 0.01)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


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
    the same three fields any other backend delivers."""
    stub_gateway.entries = completed_transcript(stub_gateway.task_id, "done")

    KubeAgentsHarness().run("check the pods")

    submission = stub_gateway.submissions[0]
    assert submission["text"] == "check the pods"
    assert submission["author"] == inject.DEFAULT_AUTHOR
    assert submission["conversation"] == stub_gateway.conversation


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
    afters = [poll["after"] for poll in stub_gateway.polls]
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


def test_a_task_nothing_executed_is_infrastructure(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway posts the placeholder itself, before the submission reaches
    the bus. A conversation showing nothing else means no executor is running
    on the addressee -- an install fault, and an empty record must not be
    graded as the agent's answer."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
    ]
    monkeypatch.setenv("AGENT_INJECT_ACCEPT_TIMEOUT", "1")

    result = KubeAgentsHarness().run("nobody is listening")

    assert result.errors and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    # Never the agent's answer: AgentResult.errored would put this text in
    # front of the judge as the reply.
    assert result.output == ""
    # And the executor is told to stop rather than left working for nobody.
    assert stub_gateway.stops


def test_a_failed_task_is_graded_rather_than_classified(
    stub_gateway: _StubGatewayServer,
) -> None:
    """A task an executor took and ended `failed` is the agent's own outcome.
    It stays in front of the judge with the terminal recorded on errors."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_POST, text="❌ failed: the cluster is unreachable", messageId=RESULT_ID),
        entry(4, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="failed"),
    ]

    result = KubeAgentsHarness().run("break")

    assert result.metadata["terminal_state"] == "failed"
    assert "the cluster is unreachable" in result.output
    assert any("ended failed" in e for e in result.errors)
    assert not any(e.startswith(harness.INFRA_FAILURE_MARKER) for e in result.errors)


def test_a_submission_the_gateway_refuses_is_infrastructure(
    stub_gateway: _StubGatewayServer,
) -> None:
    """No task means no agent saw the prompt. The commonest cause is an author
    the principal map does not carry, which is a misconfigured install rather
    than a bad answer -- and it must not be retried, because the answer cannot
    change."""
    stub_gateway.accept_submissions = False
    stub_gateway.refusal_note = "an author the principal map does not know"

    result = KubeAgentsHarness().run("who am I?")

    assert result.errors and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "principal map" in result.errors[0]
    assert result.output == ""
    assert len(stub_gateway.submissions) == 1


def test_an_unreachable_gateway_is_infrastructure_after_its_retries(
    stub_gateway: _StubGatewayServer,
) -> None:
    """A 5xx clears on its own, so it is retried; exhausting the retries is
    the run class, not an answer."""
    stub_gateway.submit_status = 503

    result = KubeAgentsHarness().run("hello")

    assert result.errors and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert len(stub_gateway.submissions) == harness._MAX_TRANSPORT_FAILURES


def test_a_deadline_stops_the_task_and_records_why(
    stub_gateway: _StubGatewayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task still running at the budget is graded on what it produced, with
    the overrun on errors -- and the executor is asked to stop."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_EDIT, text="⚙️ **working**", messageId=PLACEHOLDER_ID),
        entry(4, inject.ENTRY_POST, text="partial findings", messageId=RESULT_ID),
    ]
    monkeypatch.setenv("AGENT_INJECT_TASK_TIMEOUT", "1")

    result = KubeAgentsHarness().run("take your time")

    assert result.metadata["terminal_state"] is None
    assert result.output == "partial findings"
    assert any("did not reach a terminal state" in e for e in result.errors)
    assert not any(e.startswith(harness.INFRA_FAILURE_MARKER) for e in result.errors)
    assert stub_gateway.stops


def test_an_unknown_transport_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TRANSPORT", "carrier-pigeon")
    result = KubeAgentsHarness().run("hi")
    assert "AGENT_TRANSPORT" in result.errors[0]
    assert "carrier-pigeon" in result.errors[0]


def test_each_run_gets_its_own_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two runs sharing a conversation would have the second arrive while the
    first task is still active, where the gateway reads it as a steer rather
    than a new ask."""
    monkeypatch.delenv("AGENT_INJECT_CONVERSATION", raising=False)
    assert inject.mint_conversation() != inject.mint_conversation()
    assert inject.mint_conversation().startswith(inject.CONVERSATION_PREFIX)


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
    assert "OOMKilled" in result.output
    assert "Remediation" in result.output


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

    assert result.errors and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "never reached the bus" in result.errors[0]
    assert result.output == ""


def test_an_executor_declared_failure_is_still_graded(
    stub_gateway: _StubGatewayServer,
) -> None:
    """The other half of the pair, so the exemption cannot swallow real
    failures: an executor that took the task and failed is an answer."""
    stub_gateway.entries = [
        entry(1, inject.ENTRY_TASK, taskId=stub_gateway.task_id),
        entry(2, inject.ENTRY_POST, text=PLACEHOLDER, messageId=PLACEHOLDER_ID),
        entry(3, inject.ENTRY_POST, text="❌ failed: the cluster is unreachable",
              messageId=RESULT_ID),
        entry(4, inject.ENTRY_TERMINAL, taskId=stub_gateway.task_id, state="failed",
              source=inject.TERMINAL_SOURCE_EXECUTOR),
    ]

    result = KubeAgentsHarness().run("break")

    assert not any(e.startswith(harness.INFRA_FAILURE_MARKER) for e in result.errors)
    assert "the cluster is unreachable" in result.output


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
