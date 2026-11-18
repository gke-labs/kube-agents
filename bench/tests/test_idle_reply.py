"""The reply-after-idle replay: its prompt, the wait, and how the turns become one result."""

from __future__ import annotations

import subprocess
import sys

import pytest
from devops_bench.agents import AgentResult
from kube_agents_bench import cases, harness, idle_reply
from kube_agents_bench import inject_transport as inject

DIRECTIVE = idle_reply.DIRECTIVE
PROMPT = f"""{DIRECTIVE}
opening: The change ticket for tonight is KA-7741-ORCHID. Say OK and nothing else.
idle_seconds: 480
reply: Which change ticket did I give you?
"""
CONVERSATION = "inject:devops-bench-abc/session-recalls-context-after-idle-reap/2"


@pytest.fixture(autouse=True)
def pods(monkeypatch):
    """The session pod read: the opening's pod, then a fresh one for the reply, unless a test says otherwise."""
    seen = ["chat-heron-1", "chat-otter-2"]

    def read(conversation):
        assert conversation == CONVERSATION
        return seen.pop(0) if seen else pytest.fail("the session pod was read more than twice")

    monkeypatch.setattr(harness, "_session_pod_of", read)
    return seen


def test_an_ordinary_ask_is_not_a_replay():
    assert idle_reply.parse("check the pods on x") is None
    assert idle_reply.parse("check\n" + DIRECTIVE) is None
    assert not idle_reply.is_reply_after_idle("check the pods")


def test_the_three_fields_parse():
    assert idle_reply.parse(PROMPT) == idle_reply.Replay(
        "The change ticket for tonight is KA-7741-ORCHID. Say OK and nothing else.",
        480,
        "Which change ticket did I give you?",
    )


@pytest.mark.parametrize(
    "body",
    [
        "opening: a\nidle_seconds: 480",
        "opening: a\nidle_seconds: 480\nreply:",
        "opening: a\nidle_seconds: 480\nreply: c\nextra: d",
        "opening: a\nidle_seconds: 480\nreply: c\nnot a field",
        "opening: a\nopening: a2\nidle_seconds: 480\nreply: c",
        "opening: a\nidle_seconds: 59\nreply: c",
        "opening: a\nidle_seconds: 1801\nreply: c",
        "opening: a\nidle_seconds: 7m\nreply: c",
        "opening: a\nidle_seconds: -480\nreply: c",
        "opening: a\nidle_seconds: 480.5\nreply: c",
    ],
)
def test_a_bad_prompt_is_refused(body):
    with pytest.raises(ValueError):
        idle_reply.parse(f"{DIRECTIVE}\n{body}")


def test_the_bounds_themselves_parse():
    for seconds in (idle_reply.MIN_IDLE_SECONDS, idle_reply.MAX_IDLE_SECONDS):
        got = idle_reply.parse(f"{DIRECTIVE}\nopening: a\nidle_seconds: {seconds}\nreply: c")
        assert got is not None and got.idle_seconds == seconds


def test_a_near_miss_directive_is_refused_not_sent():
    glued = f"{DIRECTIVE} opening: a idle_seconds: 480 reply: c"
    assert idle_reply.is_reply_after_idle(glued)
    with pytest.raises(ValueError):
        idle_reply.parse(glued)


def test_the_api_transport_refuses_the_replay(monkeypatch):
    monkeypatch.setenv("AGENT_TRANSPORT", "api")
    got = harness.KubeAgentsHarness()._execute(PROMPT)
    assert f"{DIRECTIVE} needs AGENT_TRANSPORT=inject" in " ".join(got.errors)


def test_the_module_imports_without_devops_bench():
    code = (
        "import sys; sys.modules['devops_bench'] = None; sys.modules['devops_bench.agents'] = None; "
        "import kube_agents_bench.idle_reply; print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


def test_the_wait_entry_is_harness_written():
    assert idle_reply.WAIT_ENTRY in cases.HARNESS_WRITTEN_ENTRIES


def _completed_exchange() -> inject.Exchange:
    fold = inject.Fold("task-1")
    fold.mark_terminal(inject.STATE_COMPLETED, inject.TERMINAL_SOURCE_EXECUTOR, "")
    return inject.Exchange(fold, inject.OUTCOME_TERMINAL, CONVERSATION, "task-1", inject.Probe(context_id="ctx-77"))


def _opening() -> AgentResult:
    return AgentResult(
        output="OK",
        trajectory=[{"name": "a2a.status-update", "args": {"state": "completed"}}],
        tokens={},
        errors=[],
        metadata={
            "final_message": "OK",
            "task_id": "task-1",
            "conversation": CONVERSATION,
            "run_id": "devops-bench-abc",
            "case_id": "session-recalls-context-after-idle-reap",
            "repetition": "2",
        },
    )


def _answered(text: str = "KA-7741-ORCHID") -> AgentResult:
    return AgentResult(
        output=text,
        trajectory=[{"name": "a2a.status-update", "args": {"state": "completed"}}],
        tokens={},
        errors=[],
        metadata={"final_message": text, "task_id": "task-2", "outcome": inject.OUTCOME_TERMINAL},
    )


def test_the_reply_waits_then_goes_on_the_openings_conversation(monkeypatch):
    events: list = []

    def fake_inject(self, prompt, *, follow_up=None):
        events.append(("reply", prompt, follow_up))
        return _answered()

    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", fake_inject)
    monkeypatch.setattr(harness.time, "sleep", lambda s: events.append(("sleep", s)))
    replay = idle_reply.parse(PROMPT)
    got = harness.KubeAgentsHarness()._reply_after_idle(_opening(), _completed_exchange(), replay)

    assert events == [
        ("sleep", 480),
        (
            "reply",
            replay.reply,
            harness._InjectFollowUp(
                conversation=CONVERSATION,
                run_id="devops-bench-abc",
                case_id="session-recalls-context-after-idle-reap",
                repetition="2",
                suffix=idle_reply.REPLY_SUFFIX,
            ),
        ),
    ]
    assert got.output == "KA-7741-ORCHID"
    assert got.metadata["final_message"] == "KA-7741-ORCHID"
    assert [e["name"] for e in got.trajectory] == ["a2a.status-update", idle_reply.WAIT_ENTRY, "a2a.status-update"]
    assert got.trajectory[1]["args"] == {"conversation": CONVERSATION, "idle_seconds": 480}
    assert got.trajectory[1]["status"] == "completed"
    assert got.trajectory[1]["result"] == {
        "waited_seconds": got.trajectory[1]["result"]["waited_seconds"],
        "opening_pod": "chat-heron-1",
        "reply_pod": "chat-otter-2",
    }
    meta = got.metadata[idle_reply.METADATA_KEY]
    assert meta["opening_final_message"] == "OK" and meta["opening_task_id"] == "task-1"
    assert meta["wait"]["idle_seconds"] == 480
    assert meta["wait"]["opening_pod"] == "chat-heron-1" and meta["wait"]["reply_pod"] == "chat-otter-2"
    assert "waited_seconds" in meta["wait"]
    assert "pushed_report" not in got.metadata


def test_the_wait_records_how_long_it_slept(monkeypatch):
    clock = iter([100.0, 580.5])
    monkeypatch.setattr(harness.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: _answered())
    got = harness.KubeAgentsHarness()._reply_after_idle(_opening(), _completed_exchange(), idle_reply.parse(PROMPT))
    assert got.trajectory[1]["result"]["waited_seconds"] == 480.5


@pytest.mark.parametrize(
    "opening_pod, reply_pod, says",
    [
        ("chat-heron-1", "chat-heron-1", "was not reaped"),
        ("", "chat-otter-2", "after the opening"),
        ("chat-heron-1", "", "after the reply"),
        (None, "chat-otter-2", "after the opening"),
    ],
)
def test_a_reply_that_did_not_reach_a_fresh_pod_is_an_errored_wait(monkeypatch, pods, opening_pod, reply_pod, says):
    """The wait entry is what proves the rehydration: a reply on the warm pod, or no pod found, fails it."""
    pods[:] = [opening_pod, reply_pod]
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: _answered())
    got = harness.KubeAgentsHarness()._reply_after_idle(_opening(), _completed_exchange(), idle_reply.parse(PROMPT))
    entry = got.trajectory[1]
    assert entry["name"] == idle_reply.WAIT_ENTRY and entry["status"] == "error"
    assert says in entry["result"]["reason"]
    assert got.output == "KA-7741-ORCHID"


def _pods_json(*pods) -> str:
    import json

    return json.dumps({"items": [
        {"metadata": {"name": n, "annotations": {idle_reply.SESSION_KEY_ANNOTATION: k}, **({"deletionTimestamp": "x"} if gone else {})}}
        for n, k, gone in pods
    ]})


def test_session_pod_finds_the_live_pod_for_the_conversation():
    assert idle_reply.session_pod(_pods_json(("chat-a", "inject:other", False), ("chat-b", CONVERSATION, False)), CONVERSATION) == "chat-b"
    assert idle_reply.session_pod(_pods_json(("chat-a", CONVERSATION, True)), CONVERSATION) == ""
    assert idle_reply.session_pod(_pods_json(), CONVERSATION) == ""
    assert idle_reply.session_pod(None, CONVERSATION) is None
    assert idle_reply.session_pod("not json", CONVERSATION) is None


def test_an_opening_turn_that_did_not_complete_waits_for_nothing(monkeypatch):
    monkeypatch.setattr(harness.time, "sleep", lambda s: pytest.fail("the harness waited"))
    monkeypatch.setattr(
        harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: pytest.fail("the reply was sent")
    )
    fold = inject.Fold("task-1")
    fold.mark_terminal(inject.STATE_FAILED, inject.TERMINAL_SOURCE_EXECUTOR, "reason: hermes-api-refused")
    exchange = inject.Exchange(fold, inject.OUTCOME_TERMINAL, CONVERSATION, "task-1", inject.Probe(context_id="ctx-77"))
    opening = _opening()
    got = harness.KubeAgentsHarness()._reply_after_idle(opening, exchange, idle_reply.parse(PROMPT))
    assert got is opening
    assert any(DIRECTIVE in e and "failed" in e for e in got.errors)


def test_a_completed_opening_read_off_the_stream_is_infrastructure(monkeypatch):
    monkeypatch.setattr(harness.time, "sleep", lambda s: pytest.fail("the harness waited"))
    monkeypatch.setattr(
        harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: pytest.fail("the reply was sent")
    )
    fold = inject.Fold("task-1")
    fold.mark_terminal(inject.STATE_COMPLETED, inject.TERMINAL_SOURCE_EXECUTOR, "")
    exchange = inject.Exchange(fold, inject.OUTCOME_STREAM_TERMINAL, CONVERSATION, "task-1", inject.Probe())
    got = harness.KubeAgentsHarness()._reply_after_idle(_opening(), exchange, idle_reply.parse(PROMPT))
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert got.metadata.get("final_message") is None


def test_a_reply_absorbed_as_a_steer_is_sent_again_under_a_fresh_id(monkeypatch):
    suffixes: list[str] = []
    slept: list[float] = []

    def fake_inject(self, prompt, *, follow_up=None):
        suffixes.append(follow_up.suffix)
        if len(suffixes) == 1:
            return AgentResult(
                output="", trajectory=[], tokens={}, errors=["no-task"],
                metadata={"outcome": inject.OUTCOME_NOT_ACCEPTED, "refusal": inject.REFUSAL_NO_TASK},
            )
        return _answered()

    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", fake_inject)
    monkeypatch.setattr(harness.time, "sleep", lambda s: slept.append(s))
    got = harness.KubeAgentsHarness()._reply_after_idle(
        _opening(), _completed_exchange(), idle_reply.parse(PROMPT), retry_seconds=7.0
    )
    assert suffixes == ["/after-idle", "/after-idle-2"]
    assert slept == [480, 7.0]
    assert got.output == "KA-7741-ORCHID"


def test_an_infra_failed_reply_is_the_runs_infra_failure_not_a_merge(monkeypatch):
    infra = harness._infra_failure("the inject exchange failed: tunnel died")
    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute_inject", lambda self, prompt, **kw: infra)
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    got = harness.KubeAgentsHarness()._reply_after_idle(_opening(), _completed_exchange(), idle_reply.parse(PROMPT))
    assert got is infra
    assert idle_reply.METADATA_KEY not in got.metadata


def test_the_inject_path_refuses_a_bad_replay_before_sending_anything(monkeypatch):
    """_execute_inject reads the directive before any network step: an authoring error is an errored run."""
    monkeypatch.setattr(harness.KubeAgentsHarness, "_reply_after_idle", lambda *a, **k: pytest.fail("replayed"))
    bad = harness.KubeAgentsHarness()._execute_inject(f"{DIRECTIVE}\nopening: a\nidle_seconds: 5\nreply: c")
    assert bad.errors and "idle_seconds" in bad.errors[0]
