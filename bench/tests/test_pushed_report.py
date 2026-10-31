"""The reply-under-pushed-report replay: its prompt, its in-pod plant, and how the turns become one result."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from devops_bench.agents import AgentResult
from kube_agents_bench import cases, harness, pushed_report, scoring
from kube_agents_bench.worker_trajectory import HERMES_PYTHON

DIRECTIVE = pushed_report.DIRECTIVE
PROMPT = f"""{DIRECTIVE}
opening: Say OK and nothing else.
report: Capacity watch: node pool np-quartz-7 in seeded-a is at 92% allocatable CPU.
reply: Which pool did that report say was close to full?
"""


def test_an_ordinary_ask_is_not_a_replay():
    assert pushed_report.parse("check the pods on x") is None
    assert pushed_report.parse("check\n" + DIRECTIVE) is None
    assert not pushed_report.is_reply_under_pushed_report("check the pods")


def test_the_three_fields_parse():
    assert pushed_report.parse(PROMPT) == pushed_report.Replay(
        "Say OK and nothing else.",
        "Capacity watch: node pool np-quartz-7 in seeded-a is at 92% allocatable CPU.",
        "Which pool did that report say was close to full?",
    )


@pytest.mark.parametrize(
    "body",
    [
        "opening: a\nreport: b",
        "opening: a\nreport: b\nreply:",
        "opening: a\nreport: b\nreply: c\nextra: d",
        "opening: a\nreport: b\nreply: c\nnot a field",
        "opening: a\nopening: a2\nreport: b\nreply: c",
    ],
)
def test_a_bad_prompt_is_refused(body):
    with pytest.raises(ValueError):
        pushed_report.parse(f"{DIRECTIVE}\n{body}")


def test_a_near_miss_directive_is_refused_not_sent():
    """A folded or one-line prompt glues the fields onto the directive line."""
    glued = f"{DIRECTIVE} opening: Say OK. report: np-quartz-7 at 92%. reply: which pool?"
    assert pushed_report.is_reply_under_pushed_report(glued)
    with pytest.raises(ValueError):
        pushed_report.parse(glued)
    with pytest.raises(ValueError):
        pushed_report.parse(f"{DIRECTIVE} extra\nopening: a\nreport: b\nreply: c")


def test_the_api_transport_refuses_the_replay(monkeypatch):
    monkeypatch.setenv("AGENT_TRANSPORT", "api")
    got = harness.KubeAgentsHarness()._execute(PROMPT)
    assert DIRECTIVE in " ".join(got.errors)
    glued = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE} opening: a report: b reply: c")
    assert DIRECTIVE in " ".join(glued.errors)


def test_the_module_imports_without_devops_bench():
    """scripts/test_eval_rosters.py imports it into the scripts test sweep, which installs no devops_bench."""
    code = (
        "import sys; sys.modules['devops_bench'] = None; sys.modules['devops_bench.agents'] = None; "
        "import kube_agents_bench.pushed_report; print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


def test_the_plant_entry_is_harness_written():
    assert pushed_report.PLANT_ENTRY in cases.HARNESS_WRITTEN_ENTRIES


class _Incidents(BaseHTTPRequestHandler):
    rows: list[dict] = []
    auths: list[str] = []
    status = 200

    def do_POST(self):  # noqa: N802 - http.server's name
        length = int(self.headers.get("Content-Length") or 0)
        _Incidents.rows.append(json.loads(self.rfile.read(length)))
        _Incidents.auths.append(self.headers.get("Authorization") or "")
        self.send_response(_Incidents.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"stored"}')

    def log_message(self, *_):
        return


@pytest.fixture
def incidents_server():
    _Incidents.rows, _Incidents.auths, _Incidents.status = [], [], 200
    server = HTTPServer(("127.0.0.1", 0), _Incidents)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _run_plant(url: str, session: str, conversation: str, report: str, key: str = "kv-key") -> str:
    """Run the in-pod script under this interpreter against a local stand-in server."""
    command = pushed_report.plant_command(session, conversation, report)
    command = command.replace(shlex.quote(pushed_report.SESSION_KV_DEFAULT_URL), shlex.quote(url))
    command = command.replace(shlex.quote(HERMES_PYTHON), shlex.quote(sys.executable))
    env = {"PATH": "/usr/bin:/bin", "SESSION_KV_API_KEY": key}
    proc = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env, timeout=30, check=False)
    return proc.stdout


def test_the_plant_posts_the_row_the_notifier_would(incidents_server):
    out = _run_plant(incidents_server, "a2a-ctx-1", "inject:devops-bench-1/chat-reply-under-pushed-report/1", "the report")
    payload = pushed_report.plant(lambda cmd, timeout: out, "a2a-ctx-1", "x", "the report", 1.0)
    assert payload["stored"] is True
    assert _Incidents.rows == [
        {
            "chat_id": "a2a-ctx-1",
            "thread_id": "inject:devops-bench-1/chat-reply-under-pushed-report/1",
            "report": "the report",
        }
    ]
    assert _Incidents.auths == ["Bearer kv-key"]


def test_a_refused_post_is_a_broken_plant(incidents_server):
    _Incidents.status = 401
    out = _run_plant(incidents_server, "a2a-ctx-1", "c", "r")
    with pytest.raises(pushed_report.PlantBroken):
        pushed_report.plant(lambda cmd, timeout: out, "a2a-ctx-1", "c", "r", 1.0)


def test_a_script_that_never_ran_is_unavailable():
    with pytest.raises(pushed_report.PlantUnavailable):
        pushed_report.plant(lambda cmd, timeout: "", "a2a-ctx-1", "c", "r", 1.0)
    with pytest.raises(pushed_report.PlantUnavailable):
        pushed_report.plant(lambda cmd, timeout: "error: unable to upgrade connection", "a2a-ctx-1", "c", "r", 1.0)


def test_a_reply_that_is_not_json_is_broken():
    with pytest.raises(pushed_report.PlantBroken):
        pushed_report.plant(lambda cmd, timeout: f"{pushed_report.SENTINEL}\nTraceback", "a2a-ctx-1", "c", "r", 1.0)


def test_merge_grades_the_reply_turn_and_keeps_both_trajectories():
    opening = AgentResult(
        output="OK",
        trajectory=[{"name": "a2a.status-update", "args": {"state": "completed"}}],
        tokens={},
        errors=["first"],
        metadata={"final_message": "OK", "task_id": "task-1", "conversation": "c"},
    )
    reply = AgentResult(
        output="np-quartz-7, at 92%.",
        trajectory=[{"name": "a2a.status-update", "args": {"state": "working"}}],
        tokens={"total": 1},
        errors=["second"],
        metadata={"final_message": "np-quartz-7, at 92%.", "task_id": "task-2", "conversation": "c"},
    )
    entry = pushed_report.plant_entry("a2a-ctx-1", "inject:c", {"stored": True, "status": 200})
    merged = pushed_report.merge(opening, reply, entry)
    assert merged.output == "np-quartz-7, at 92%."
    assert merged.metadata["final_message"] == "np-quartz-7, at 92%."
    assert merged.metadata["task_id"] == "task-2"
    assert merged.metadata["pushed_report"]["opening_final_message"] == "OK"
    assert merged.metadata["pushed_report"]["opening_task_id"] == "task-1"
    assert [e["name"] for e in merged.trajectory] == ["a2a.status-update", pushed_report.PLANT_ENTRY, "a2a.status-update"]
    assert merged.trajectory[1]["status"] == "completed"
    assert merged.trajectory[1]["args"] == {"session": "a2a-ctx-1", "conversation": "inject:c"}
    assert type(merged) is AgentResult
    assert merged.errors == ["first", "second"]
    assert merged.tokens == {"total": 1}


def test_merge_sums_both_turns_tokens_bucket_by_bucket():
    """An inject record's tokens are the chain's usage; the run's are both turns'."""
    opening = AgentResult(
        output="OK", trajectory=[], tokens={"input": 10, "output": 2, "total": 12, "cached": None,
                                              "children": {"input": 3, "output": 1, "total": 4}},
        errors=[], metadata={},
    )
    reply = AgentResult(
        output="np-quartz-7", trajectory=[], tokens={"input": 100, "output": 20, "total": 120, "cached": None,
                                                     "children": {"input": 30, "output": 10, "total": 40}, "usage_partial": True},
        errors=[], metadata={},
    )
    merged = pushed_report.merge(opening, reply, pushed_report.plant_entry("a2a-ctx-1", "inject:c", {"stored": True}))
    assert merged.tokens == {"input": 110, "output": 22, "total": 132, "cached": None,
                             "children": {"input": 33, "output": 11, "total": 44}, "usage_partial": True}
    assert opening.tokens["total"] == 12 and reply.tokens["total"] == 120


# The two-turn orchestration in the harness: the opening turn's conversation
# and identity are what the reply is sent on, the plant is keyed to the
# conversation's Hermes session, and the run is graded on the reply.
from kube_agents_bench import inject_transport as inject  # noqa: E402


def _completed_exchange(conversation: str, context_id: str) -> inject.Exchange:
    fold = inject.Fold("task-1")
    fold.mark_terminal(inject.STATE_COMPLETED, inject.TERMINAL_SOURCE_EXECUTOR, "")
    probe = inject.Probe(context_id=context_id)
    return inject.Exchange(fold, inject.OUTCOME_TERMINAL, conversation, "task-1", probe)


def _opening(conversation: str) -> AgentResult:
    return AgentResult(
        output="OK",
        trajectory=[{"name": "a2a.status-update", "args": {"state": "completed"}}],
        tokens={},
        errors=[],
        metadata={
            "final_message": "OK",
            "task_id": "task-1",
            "conversation": conversation,
            "run_id": "devops-bench-abc",
            "case_id": "chat-reply-under-pushed-report",
            "repetition": "2",
            "message_id": "devops-bench-abc/chat-reply-under-pushed-report/2",
        },
    )


def _stored(cmd: str, timeout: float) -> str:
    return f"{pushed_report.SENTINEL}\n" + json.dumps({"stored": True, "status": 200})


def test_the_reply_goes_on_the_openings_conversation_with_its_identity(monkeypatch):
    conversation = "inject:devops-bench-abc/chat-reply-under-pushed-report/2"
    shells: list[str] = []
    follow_ups: list = []

    def shell(cmd: str, timeout: float) -> str:
        shells.append(cmd)
        return _stored(cmd, timeout)

    reply_result = AgentResult(
        output="np-quartz-7, at 92%.", trajectory=[{"name": "a2a.status-update", "args": {"state": "completed"}}],
        tokens={}, errors=[], metadata={"final_message": "np-quartz-7, at 92%.", "task_id": "task-2"},
    )

    def fake_inject(self, prompt, *, follow_up=None):
        follow_ups.append((prompt, follow_up))
        return reply_result

    monkeypatch.setattr(harness, "_agent_shell", shell)
    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", fake_inject)
    replay = pushed_report.parse(PROMPT)
    got = harness.KubeAgentsHarness()._reply_under_pushed_report(
        _opening(conversation), _completed_exchange(conversation, "ctx-77"), replay
    )

    assert len(shells) == 1
    assert shlex.quote("a2a-ctx-77") in shells[0] or "a2a-ctx-77" in shells[0]
    assert conversation in shells[0]
    assert replay.report in shells[0]
    assert follow_ups == [
        (
            replay.reply,
            harness._InjectFollowUp(
                conversation=conversation,
                run_id="devops-bench-abc",
                case_id="chat-reply-under-pushed-report",
                repetition="2",
                suffix=pushed_report.REPLY_SUFFIX,
            ),
        )
    ]
    assert got.output == "np-quartz-7, at 92%."
    assert got.metadata["final_message"] == "np-quartz-7, at 92%."
    assert [e["name"] for e in got.trajectory] == ["a2a.status-update", pushed_report.PLANT_ENTRY, "a2a.status-update"]
    assert got.trajectory[1]["args"] == {"session": "a2a-ctx-77", "conversation": conversation}
    assert got.metadata["pushed_report"]["opening_final_message"] == "OK"


def test_an_opening_turn_that_did_not_complete_sends_nothing(monkeypatch):
    conversation = "inject:devops-bench-abc/chat-reply-under-pushed-report/2"
    monkeypatch.setattr(harness, "_agent_shell", lambda cmd, timeout: pytest.fail("the plant ran"))
    monkeypatch.setattr(
        harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: pytest.fail("the reply was sent")
    )
    fold = inject.Fold("task-1")
    fold.mark_terminal(inject.STATE_FAILED, inject.TERMINAL_SOURCE_EXECUTOR, "reason: hermes-api-refused")
    exchange = inject.Exchange(fold, inject.OUTCOME_TERMINAL, conversation, "task-1", inject.Probe(context_id="ctx-77"))
    opening = _opening(conversation)
    got = harness.KubeAgentsHarness()._reply_under_pushed_report(opening, exchange, pushed_report.parse(PROMPT))
    assert got is opening
    assert any(pushed_report.DIRECTIVE in e and "failed" in e for e in got.errors)


def test_a_door_with_no_context_id_is_infrastructure(monkeypatch):
    conversation = "inject:devops-bench-abc/chat-reply-under-pushed-report/2"
    monkeypatch.setattr(harness, "_agent_shell", lambda cmd, timeout: pytest.fail("the plant ran"))
    monkeypatch.setattr(
        harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: pytest.fail("the reply was sent")
    )
    got = harness.KubeAgentsHarness()._reply_under_pushed_report(
        _opening(conversation), _completed_exchange(conversation, ""), pushed_report.parse(PROMPT)
    )
    assert got.errors and "contextId" in " ".join(got.errors)
    assert got.metadata.get("final_message") is None


def test_a_broken_plant_is_an_errored_run_and_sends_no_reply(monkeypatch):
    conversation = "inject:devops-bench-abc/chat-reply-under-pushed-report/2"
    monkeypatch.setattr(
        harness, "_agent_shell",
        lambda cmd, timeout: f"{pushed_report.SENTINEL}\n" + json.dumps({"stored": False, "error": "HTTPError: 401"}),
    )
    monkeypatch.setattr(
        harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: pytest.fail("the reply was sent")
    )
    got = harness.KubeAgentsHarness()._reply_under_pushed_report(
        _opening(conversation), _completed_exchange(conversation, "ctx-77"), pushed_report.parse(PROMPT)
    )
    assert got.errors and "401" in " ".join(got.errors)


def test_a_follow_up_reuses_the_conversation_and_suffixes_the_message_id(monkeypatch):
    """The inject path itself, driven to its POST: a follow-up mints nothing anew."""
    seen: dict = {}

    class FakeTask:
        """Records what the POST would carry, then fails the exchange before any request."""

        def __init__(self, *, base_url, conversation, prompt, token, author=None, message_id=""):
            seen.update(conversation=conversation, prompt=prompt, message_id=message_id)
            self.conversation = conversation
            self.task_id = ""
            self.first_event_grace = 0.0
            self.unanswered_post = False
            self.inject_only = False
            self.backend = ""

        def preflight(self):
            raise inject.InjectUnavailable("stop here", retryable=False)

        def recover_task_id(self):
            return ""

    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    monkeypatch.setenv("AGENT_INJECT_TOKEN", "tok")
    monkeypatch.setenv("AGENT_INJECT_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(harness.inject, "InjectTask", FakeTask)
    monkeypatch.setattr(harness, "_inject_identity", lambda: pytest.fail("a follow-up minted a new identity"))
    follow_up = harness._InjectFollowUp(
        conversation="inject:devops-bench-abc/chat-reply-under-pushed-report/2",
        run_id="devops-bench-abc",
        case_id="chat-reply-under-pushed-report",
        repetition="2",
        suffix=pushed_report.REPLY_SUFFIX,
    )
    got = harness.KubeAgentsHarness()._execute_inject("which pool?", follow_up=follow_up)
    assert got.errors
    assert seen == {
        "conversation": "inject:devops-bench-abc/chat-reply-under-pushed-report/2",
        "prompt": "which pool?",
        "message_id": "devops-bench-abc/chat-reply-under-pushed-report/2/reply",
    }


def test_a_completed_opening_read_off_the_stream_is_infrastructure(monkeypatch):
    """The deliverable is the reply turn, which cannot be sent on a record the relay still holds."""
    conversation = "inject:devops-bench-abc/chat-reply-under-pushed-report/2"
    monkeypatch.setattr(harness, "_agent_shell", lambda cmd, timeout: pytest.fail("the plant ran"))
    monkeypatch.setattr(
        harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: pytest.fail("the reply was sent")
    )
    fold = inject.Fold("task-1")
    fold.mark_terminal(inject.STATE_COMPLETED, inject.TERMINAL_SOURCE_EXECUTOR, "")
    exchange = inject.Exchange(fold, inject.OUTCOME_STREAM_TERMINAL, conversation, "task-1", inject.Probe(context_id="ctx-77"))
    got = harness.KubeAgentsHarness()._reply_under_pushed_report(_opening(conversation), exchange, pushed_report.parse(PROMPT))
    assert got.errors and inject.OUTCOME_STREAM_TERMINAL in " ".join(got.errors)
    assert got.metadata.get("final_message") is None


def _absorbed(conversation: str) -> AgentResult:
    return AgentResult(
        output="", trajectory=[], tokens={}, errors=["no-task"],
        metadata={"outcome": inject.OUTCOME_NOT_ACCEPTED, "refusal": inject.REFUSAL_NO_TASK, "conversation": conversation},
    )


def test_a_reply_absorbed_as_a_steer_is_sent_again_under_a_fresh_id(monkeypatch):
    conversation = "inject:devops-bench-abc/chat-reply-under-pushed-report/2"
    suffixes: list[str] = []
    slept: list[float] = []
    answered = AgentResult(
        output="np-quartz-7, at 92%.", trajectory=[], tokens={}, errors=[],
        metadata={"final_message": "np-quartz-7, at 92%.", "task_id": "task-2", "outcome": inject.OUTCOME_TERMINAL},
    )

    def fake_inject(self, prompt, *, follow_up=None):
        suffixes.append(follow_up.suffix)
        return _absorbed(conversation) if len(suffixes) == 1 else answered

    monkeypatch.setattr(harness, "_agent_shell", _stored)
    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", fake_inject)
    monkeypatch.setattr(harness.time, "sleep", lambda s: slept.append(s))
    got = harness.KubeAgentsHarness()._reply_under_pushed_report(
        _opening(conversation), _completed_exchange(conversation, "ctx-77"), pushed_report.parse(PROMPT), retry_seconds=7.0
    )
    assert suffixes == ["/reply", "/reply-2"]
    assert slept == [7.0]
    assert got.output == "np-quartz-7, at 92%."


def test_a_reply_absorbed_three_times_is_infrastructure(monkeypatch):
    conversation = "inject:devops-bench-abc/chat-reply-under-pushed-report/2"
    suffixes: list[str] = []

    def fake_inject(self, prompt, *, follow_up=None):
        suffixes.append(follow_up.suffix)
        return _absorbed(conversation)

    monkeypatch.setattr(harness, "_agent_shell", _stored)
    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", fake_inject)
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    got = harness.KubeAgentsHarness()._reply_under_pushed_report(
        _opening(conversation), _completed_exchange(conversation, "ctx-77"), pushed_report.parse(PROMPT)
    )
    assert suffixes == ["/reply", "/reply-2", "/reply-3"]
    assert got.errors and "absorbed" in " ".join(got.errors)
    assert got.metadata.get("final_message") is None


def test_an_infra_failed_reply_is_the_runs_infra_failure_not_a_merge(monkeypatch):
    """A reply turn lost in transport keeps _infra_failure's shape: no output, no
    trajectory, the marker alone; the opening's completed task is not merged in."""
    conversation = "inject:devops-bench-abc/chat-reply-under-pushed-report/2"
    infra = harness._infra_failure("the inject exchange failed: tunnel died")
    monkeypatch.setattr(harness, "_agent_shell", _stored)
    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: infra)
    got = harness.KubeAgentsHarness()._reply_under_pushed_report(
        _opening(conversation), _completed_exchange(conversation, "ctx-77"), pushed_report.parse(PROMPT)
    )
    assert got is infra
    assert got.output == "" and got.trajectory == []
    assert "task_id" not in got.metadata and "pushed_report" not in got.metadata
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)


def test_every_activity_marker_must_vouch_on_a_two_turn_record():
    clean = {"name": scoring.INJECT_ACTIVITY_EVENT, "args": {"dropped": 0}}
    lossy = {"name": scoring.INJECT_ACTIVITY_EVENT, "args": {"dropped": 2}}
    done = {"name": scoring.A2A_STATUS_EVENT, "args": {"state": "completed", "final": True}}
    working = {"name": scoring.A2A_STATUS_EVENT, "args": {"state": "working", "final": False}}
    assert scoring._inject_trace_vouched([working, done, clean, done, clean])
    assert not scoring._inject_trace_vouched([done, clean, done, lossy])
    assert not scoring._inject_trace_vouched([done, lossy, done, clean])
    # A task whose reads never carried the trace writes no marker: one marker
    # for two tasks vouches for neither.
    assert not scoring._inject_trace_vouched([done, done, clean])
    assert scoring._inject_trace_vouched([done, clean])
    assert not scoring._inject_trace_vouched([done])
    assert not scoring._inject_trace_vouched([])


def test_a_two_turn_record_with_no_marker_is_blind():
    """The plant entry is the harness's, not a call the door showed: a merged
    record whose door carried no trace is blind like a single-turn one."""
    task = {"name": scoring.INJECT_TASK_EVENT, "args": {}}
    done = {"name": scoring.A2A_STATUS_EVENT, "args": {"state": "completed", "final": True}}
    plant = pushed_report.plant_entry("a2a-ctx-1", "inject:c", {"stored": True})
    assert scoring._inject_blind([task, done, plant, task, done])
    clean = {"name": scoring.INJECT_ACTIVITY_EVENT, "args": {"dropped": 0}}
    assert not scoring._inject_blind([task, done, clean, plant, task, done, clean])
    assert scoring._inject_blind([task, done, clean, plant, task, done])


def test_a_follow_up_the_door_absorbed_records_the_refusal(monkeypatch):
    """The inject path itself: a follow-up the door answers no-task to is a not-accepted
    result carrying the refusal the retry keys on, not a transport error."""

    class AbsorbedTask:
        def __init__(self, *, base_url, conversation, prompt, token, author=None, message_id=""):
            self.conversation = conversation
            self.task_id = ""
            self.first_event_grace = 1.0
            self.unanswered_post = False
            self.inject_only = False
            self.backend = "inject"
            self.refusal = ""
            self.note = ""
            self.cancel_sent = False

        def preflight(self):
            return inject.Probe(grace_seconds=1.0)

        def submit(self):
            self.refusal = inject.REFUSAL_NO_TASK
            self.note = "steered the active task"
            return ""

    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    monkeypatch.setenv("AGENT_INJECT_TOKEN", "tok")
    monkeypatch.setenv("AGENT_INJECT_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(harness.inject, "InjectTask", AbsorbedTask)
    monkeypatch.setattr(harness, "_inject_identity", lambda: pytest.fail("a follow-up minted a new identity"))
    follow_up = harness._InjectFollowUp(
        conversation="inject:devops-bench-abc/chat-reply-under-pushed-report/2",
        run_id="devops-bench-abc", case_id="chat-reply-under-pushed-report", repetition="2",
        suffix=pushed_report.REPLY_SUFFIX,
    )
    got = harness.KubeAgentsHarness()._execute_inject("which pool?", follow_up=follow_up)
    assert got.metadata["outcome"] == inject.OUTCOME_NOT_ACCEPTED
    assert got.metadata["refusal"] == inject.REFUSAL_NO_TASK
    assert got.errors and "steered the active task" in got.errors[0]
    assert "final_message" in got.metadata


def test_the_plant_speaks_the_kv_servers_contract():
    """The row's route, keys and default address are the notifier's and the handler's,
    read from their source rather than from a stand-in."""
    repo = Path(__file__).resolve().parents[2]
    server = (repo / "agents" / "platform" / "scripts" / "session_kv_server.py").read_text(encoding="utf-8")
    notifier = (repo / "deploy" / "docker" / "patches" / "kanban_notifier.py").read_text(encoding="utf-8")
    import re

    decorator = f'@app.post("{pushed_report.INCIDENTS_PATH}"'
    assert decorator in server
    handler = server[server.index(decorator) + len(decorator):]
    handler = handler[: handler.index("\n@app.")]
    for key in ("chat_id", "thread_id", "report"):
        assert re.search(r'body\.get\("%s"\)' % key, handler), f"the handler no longer reads {key}"
    assert '{"chat_id": SESSION, "thread_id": CONVERSATION, "report": REPORT}' in pushed_report._PLANT_SCRIPT
    assert f'SESSION_KV_URL = "{pushed_report.SESSION_KV_DEFAULT_URL}"' in notifier
    assert f'f"{{SESSION_KV_URL}}{pushed_report.INCIDENTS_PATH}"' in notifier
