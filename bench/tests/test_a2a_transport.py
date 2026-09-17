"""The a2a transport: envelopes, the fold, and the harness's mapping of an
exchange onto the run record -- with the bus itself replaced by a fake, since
no nats-server runs in CI.

``test_harness.py`` covers the api transport and stays untouched: selecting
``AGENT_TRANSPORT=a2a`` must leave every one of those paths as they were.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import pytest

from kube_agents_bench import a2a_transport as a2a
from kube_agents_bench import harness, scoring
from kube_agents_bench.cases import load_case
from kube_agents_bench.harness import KubeAgentsHarness
from kube_agents_bench.scoring import Rung, grade_case

_ADDRESSEE = "platform"
_PROMPT = "Find the crashlooping deployment and report the root cause."
_ANSWER = "payments-api in seeded-debug is OOMKilled at its 64Mi limit."


# --------------------------------------------------------------------------
# Envelopes and subjects
# --------------------------------------------------------------------------


def test_subjects_follow_the_0_4_layout() -> None:
    assert a2a.task_in_subject("platform", "task-1") == "a2a.tasks.platform.task-1.in"
    assert a2a.task_events_subject("platform", "task-1") == "a2a.tasks.platform.task-1.events"
    assert (
        a2a.task_supervisor_subject("platform", "task-1") == "a2a.tasks.platform.task-1.supervisor"
    )


def test_minted_ids_have_the_gateway_shape() -> None:
    ids = a2a.mint_ids()
    # ``task-`` + 16 hex: randHex(8) in the gateway. Dot-free DNS-1123 labels,
    # which is what the subject grammar requires of a taskId token.
    assert ids.task_id.startswith("task-") and len(ids.task_id) == len("task-") + 16
    assert ids.context_id.startswith("ctx-") and len(ids.context_id) == len("ctx-") + 24
    assert ids.correlation_id.startswith("corr-") and len(ids.correlation_id) == len("corr-") + 24
    assert "." not in ids.task_id


def test_a_follow_up_keeps_context_and_correlation() -> None:
    first = a2a.mint_ids()
    follow = a2a.mint_ids(context_id=first.context_id, correlation_id=first.correlation_id)
    assert follow.task_id != first.task_id
    assert follow.context_id == first.context_id
    assert follow.correlation_id == first.correlation_id


def test_the_submission_envelope_matches_the_spec() -> None:
    ids = a2a.mint_ids()
    env = a2a.build_envelope(
        a2a.KIND_MESSAGE, ids, a2a.message_payload(_PROMPT, ids), to=_ADDRESSEE
    )
    assert env["protocol"] == "a2a-jetstream/0.4"
    assert env["kind"] == "message"
    assert env["taskId"] == ids.task_id
    assert env["contextId"] == ids.context_id
    assert env["correlationId"] == ids.correlation_id
    assert env["to"] == {"session": _ADDRESSEE}
    # Reserved: a client never populates identity; authority is the gateway's.
    assert env["identity"] is None
    assert env["authority"] is None
    # from must not name the addressee, or the in-subject agreement check
    # refuses the envelope as an executor writing its own in subject.
    assert env["from"]["session"] != _ADDRESSEE
    assert env["envelopeId"].startswith("env-")
    assert env["ts"].endswith("Z")
    payload = env["payload"]
    assert payload["role"] == "user"
    assert payload["parts"] == [{"kind": "text", "text": _PROMPT}]
    assert payload["messageId"].startswith("msg-")
    assert payload["taskId"] == ids.task_id and payload["contextId"] == ids.context_id


def test_a_cancel_carries_the_empty_object() -> None:
    ids = a2a.mint_ids()
    env = a2a.build_envelope(a2a.KIND_CANCEL, ids, a2a.cancel_payload(), to=_ADDRESSEE)
    assert env["kind"] == "cancel"
    assert env["payload"] == {}
    assert env["taskId"] == ids.task_id


# --------------------------------------------------------------------------
# The fold
# --------------------------------------------------------------------------


def _status(task_id: str, state: str, *, final: bool = False, text: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "taskId": task_id,
        "contextId": "ctx-x",
        "status": {"state": state},
        "final": final,
    }
    if text:
        payload["status"]["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": text}],
            "messageId": "msg-x",
        }
    return {"taskId": task_id, "kind": "status-update", "payload": payload}


def _artifact(
    task_id: str,
    name: str,
    parts: list[dict[str, Any]],
    *,
    append: bool = False,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    return {
        "taskId": task_id,
        "kind": "artifact-update",
        "payload": {
            "taskId": task_id,
            "contextId": "ctx-x",
            "artifact": {
                "artifactId": artifact_id or f"artifact-{name}",
                "name": name,
                "parts": parts,
            },
            "append": append,
        },
    }


def _lifecycle(task_id: str, answer: str = _ANSWER) -> list[dict[str, Any]]:
    """The bridge's sequence: submitted, working, result, completed."""
    return [
        _status(task_id, "submitted"),
        _status(task_id, "working"),
        _artifact(task_id, "result", [{"kind": "text", "text": answer}]),
        _status(task_id, "completed", final=True),
    ]


def test_fold_materialises_the_bridge_lifecycle() -> None:
    fold = a2a.Fold("task-1")
    for env in _lifecycle("task-1"):
        fold.apply(env)
    assert fold.final and fold.state == "completed"
    assert fold.history == ["submitted", "working", "completed"]
    assert fold.artifact_text("result") == _ANSWER
    assert fold.accepted
    names = [e["name"] for e in fold.trajectory]
    assert names == [
        "a2a.status-update",
        "a2a.status-update",
        "a2a.artifact-update",
        "a2a.status-update",
    ]
    assert fold.trajectory[-1]["args"] == {"state": "completed", "final": True}


def test_fold_appends_chunked_artifacts_and_replaces_whole_ones() -> None:
    fold = a2a.Fold("task-1")
    fold.apply(_artifact("task-1", "result", [{"kind": "text", "text": "one "}]))
    fold.apply(_artifact("task-1", "result", [{"kind": "text", "text": "two"}], append=True))
    assert fold.artifact_text("result") == "one two"
    fold.apply(_artifact("task-1", "result", [{"kind": "text", "text": "replaced"}]))
    assert fold.artifact_text("result") == "replaced"


def test_fold_drops_events_after_the_terminal_one() -> None:
    fold = a2a.Fold("task-1")
    for env in _lifecycle("task-1"):
        fold.apply(env)
    fold.apply(_status("task-1", "failed", final=True))
    fold.apply(_artifact("task-1", "result", [{"kind": "text", "text": "late"}]))
    assert fold.state == "completed"
    assert fold.artifact_text("result") == _ANSWER
    assert fold.post_final_dropped == 2


def test_fold_skips_what_does_not_parse_and_counts_it() -> None:
    fold = a2a.Fold("task-1")
    fold.apply("not an envelope")
    fold.apply(_status("task-2", "working"))  # another task's event
    fold.apply({"taskId": "task-1", "kind": "message", "payload": {}})  # not an event kind
    fold.apply({"taskId": "task-1", "kind": "status-update", "payload": {"status": {}}})
    assert fold.malformed == 4
    assert not fold.accepted


def test_fold_reads_the_terminal_status_message() -> None:
    fold = a2a.Fold("task-1")
    fold.apply(_status("task-1", "submitted"))
    fold.apply(_status("task-1", "failed", final=True, text="exit 1: hermes: no such profile"))
    assert fold.state == "failed"
    assert fold.status_message == "exit 1: hermes: no such profile"


def test_fold_turns_activity_data_parts_into_tool_calls() -> None:
    """An executor that publishes its tool trace makes the tool verifiers work."""
    fold = a2a.Fold("task-1")
    fold.apply(
        _artifact(
            "task-1",
            "activity",
            [
                {
                    "kind": "data",
                    "data": {
                        "name": "kanban_create",
                        "args": {"title": "investigate"},
                        "result": '{"task_id": "card-7"}',
                        "status": "completed",
                    },
                },
                {"kind": "data", "data": {"note": "no tool name here"}},
            ],
        )
    )
    calls = [e for e in fold.trajectory if e["name"] == "kanban_create"]
    assert calls == [
        {
            "name": "kanban_create",
            "args": {"title": "investigate"},
            "result": '{"task_id": "card-7"}',
            "status": "completed",
        }
    ]
    # The activity artifact itself is not doubled as a lifecycle entry.
    assert not any(e["name"] == "a2a.artifact-update" for e in fold.trajectory)


def test_fold_keeps_progress_text_on_its_entry() -> None:
    fold = a2a.Fold("task-1")
    fold.apply(_artifact("task-1", "progress", [{"kind": "text", "text": "reading events"}]))
    entry = fold.trajectory[-1]
    assert entry["name"] == "a2a.artifact-update"
    assert entry["args"]["artifact"] == "progress"
    assert entry["result"] == "reading events"


# --------------------------------------------------------------------------
# The harness on the a2a transport, with the bus faked
# --------------------------------------------------------------------------


class _FakeBus:
    """Stands in for ``a2a.BusTask``: scripted events, outcomes and failures.

    ``script`` is consumed one entry per exchange. An entry is either a list of
    events (folded, outcome terminal unless ``outcome`` overrides it) or an
    exception to raise. The harness constructs one instance per task, so the
    class-level script is what a test programs.
    """

    script: ClassVar[list[Any]] = []
    outcome: str = a2a.OUTCOME_TERMINAL
    instances: ClassVar[list[_FakeBus]] = []

    def __init__(
        self,
        *,
        url: str,
        password: str,
        addressee: str,
        ids: a2a.TaskIds,
        prompt: str,
        user: str = a2a.NATS_USER,
    ) -> None:
        self.url = url
        self.password = password
        self.addressee = addressee
        self.ids = ids
        self.prompt = prompt
        self.user = user
        self.attempts: list[tuple[float, float]] = []
        _FakeBus.instances.append(self)

    def exchange(self, *, accept_timeout: float, deadline: float) -> a2a.Exchange:
        self.attempts.append((accept_timeout, deadline))
        step = _FakeBus.script.pop(0) if _FakeBus.script else _lifecycle(self.ids.task_id)
        if isinstance(step, BaseException):
            raise step
        fold = a2a.Fold(self.ids.task_id)
        events = []
        for raw in step:
            env = dict(raw)
            env["taskId"] = self.ids.task_id
            env["payload"] = {**env["payload"], "taskId": self.ids.task_id}
            fold.apply(env)
            events.append(env)
        return a2a.Exchange(fold=fold, outcome=_FakeBus.outcome, events=events)


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch):
    _FakeBus.script = []
    _FakeBus.outcome = a2a.OUTCOME_TERMINAL
    _FakeBus.instances = []
    monkeypatch.setattr(harness.a2a, "BusTask", _FakeBus)
    monkeypatch.setenv("AGENT_TRANSPORT", "a2a")
    # No cluster in the test: the URL and password overrides skip the
    # port-forward and the Secret read, which is also what a developer with a
    # forward of their own uses.
    monkeypatch.setenv("AGENT_A2A_NATS_URL", "nats://127.0.0.1:1")
    monkeypatch.setenv("AGENT_A2A_NATS_PASSWORD", "test-password")
    monkeypatch.setenv("AGENT_DELEGATION_TIMEOUT", "0")
    resets: list[tuple[Any, ...]] = []
    monkeypatch.setattr(harness, "_reset_port_forward", lambda *a, **k: resets.append((a, k)))
    return _FakeBus


def test_a_completed_task_is_the_answer(fake_bus) -> None:
    result = KubeAgentsHarness().run(_PROMPT)

    assert not result.has_errors()
    assert result.output == _ANSWER
    assert result.metadata["final_message"] == _ANSWER
    assert result.metadata["transport"] == "a2a"
    assert result.metadata["terminal_state"] == "completed"
    assert result.metadata["status_history"] == ["submitted", "working", "completed"]
    assert result.metadata["artifacts"] == ["result"]
    # No usage on the bus: every bucket null, and the record says why.
    assert all(v is None for v in result.tokens.values())
    assert "no token usage" in result.metadata["tokens_note"]
    # The bus half saw exactly the prompt, addressed to the default executor.
    (task,) = fake_bus.instances
    assert task.prompt == _PROMPT
    assert task.addressee == "platform"
    assert task.user == "gateway"
    assert task.url == "nats://127.0.0.1:1"
    assert result.metadata["task_id"] == task.ids.task_id


def test_the_transcript_is_stashed_for_the_verifiers(fake_bus) -> None:
    from kube_agents_bench import transcript

    KubeAgentsHarness().run(_PROMPT)
    snap = transcript.get()
    assert snap is not None
    assert snap.output == _ANSWER
    assert snap.final_message == _ANSWER
    assert [e["name"] for e in snap.trajectory][-1] == "a2a.status-update"


def test_the_addressee_is_configurable(fake_bus, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_A2A_ADDRESSEE", "cluster-seeded-a")
    result = KubeAgentsHarness().run(_PROMPT)
    assert not result.has_errors()
    assert fake_bus.instances[0].addressee == "cluster-seeded-a"
    assert result.metadata["addressee"] == "cluster-seeded-a"


def test_a_failed_terminal_is_graded_not_infrastructure(fake_bus) -> None:
    """An executor took the task and failed it: that is the agent's outcome."""
    fake_bus.script = [
        [
            _status("t", "submitted"),
            _status("t", "working"),
            _status("t", "failed", final=True, text="exit 1: model refused"),
        ]
    ]
    result = KubeAgentsHarness().run(_PROMPT)

    assert result.has_errors()
    assert not result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "ended failed" in result.errors[0]
    assert "model refused" in result.errors[0]
    assert result.output == ""
    assert result.metadata["terminal_state"] == "failed"


def test_no_executor_is_infrastructure(fake_bus) -> None:
    """Nothing consumed the submission: the run class, not an empty answer."""
    fake_bus.script = [[]]
    fake_bus.outcome = a2a.OUTCOME_NOT_ACCEPTED
    result = KubeAgentsHarness().run(_PROMPT)

    assert result.has_errors()
    assert result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "no executor accepted" in result.errors[0]
    assert "cancel published" in result.errors[0]
    assert result.output == ""
    assert result.trajectory == []


def test_a_task_still_running_at_the_deadline_is_graded_with_the_error(
    fake_bus,
) -> None:
    fake_bus.script = [
        [
            _status("t", "submitted"),
            _status("t", "working"),
            _artifact("t", "progress", [{"kind": "text", "text": "still reading"}]),
        ]
    ]
    fake_bus.outcome = a2a.OUTCOME_DEADLINE
    result = KubeAgentsHarness().run(_PROMPT)

    assert result.has_errors()
    assert not result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "did not reach a terminal state" in result.errors[0]
    assert "'working'" in result.errors[0]
    assert result.metadata["terminal_state"] is None


def test_an_unreachable_bus_is_retried_through_a_fresh_tunnel_then_infrastructure(
    fake_bus, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets: list[dict[str, Any]] = []
    monkeypatch.delenv("AGENT_A2A_NATS_URL")
    monkeypatch.setenv("AGENT_A2A_LOCAL_PORT", "24999")
    monkeypatch.setattr(harness, "_ensure_port_forward", lambda *a, **k: None)
    monkeypatch.setattr(harness, "_reset_port_forward", lambda *a, **k: resets.append(k))
    fake_bus.script = [
        a2a.BusUnavailable("connect refused"),
        a2a.BusUnavailable("connect refused"),
        a2a.BusUnavailable("connect refused"),
    ]
    result = KubeAgentsHarness().run(_PROMPT)

    assert result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "connect refused" in result.errors[0]
    (task,) = fake_bus.instances
    assert len(task.attempts) == harness._MAX_TRANSPORT_FAILURES
    # The respawn targets the NATS Service on its client port, not the agent.
    assert len(resets) == harness._MAX_TRANSPORT_FAILURES - 1
    assert all(r == {"service": "platform-agent-a2a-nats", "remote_port": 4222} for r in resets)
    assert task.url == "nats://127.0.0.1:24999"


def test_a_bus_that_comes_back_on_retry_reaches_the_answer(fake_bus) -> None:
    fake_bus.script = [a2a.BusUnavailable("connection stayed down"), _lifecycle("t")]
    result = KubeAgentsHarness().run(_PROMPT)

    assert not result.has_errors()
    assert result.output == _ANSWER
    assert len(fake_bus.instances[0].attempts) == 2


def test_a_refused_credential_is_not_retried(fake_bus) -> None:
    fake_bus.script = [a2a.BusUnavailable("Authorization Violation", retryable=False)]
    result = KubeAgentsHarness().run(_PROMPT)

    assert result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert len(fake_bus.instances[0].attempts) == 1


def test_a_missing_credential_is_infrastructure(fake_bus, monkeypatch: pytest.MonkeyPatch) -> None:
    """No creds Secret means no bus on this install (mode today)."""
    monkeypatch.delenv("AGENT_A2A_NATS_PASSWORD")
    monkeypatch.setattr(
        harness,
        "_a2a_password",
        lambda svc: (_ for _ in ()).throw(
            RuntimeError(f"no bus credential: secret/{svc}-creds not found")
        ),
    )
    result = KubeAgentsHarness().run(_PROMPT)

    assert result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "platform-agent-a2a-nats-creds" in result.errors[0]
    assert fake_bus.instances == []


def test_the_deadline_is_the_http_timeout(fake_bus, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HTTP_TIMEOUT", "42")
    monkeypatch.setenv("AGENT_A2A_ACCEPT_TIMEOUT", "7")
    before = time.monotonic()
    KubeAgentsHarness().run(_PROMPT)
    ((accept_timeout, deadline),) = fake_bus.instances[0].attempts
    assert accept_timeout == 7
    assert before + 41 < deadline <= before + 43


def test_an_unknown_transport_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TRANSPORT", "carrier-pigeon")
    result = KubeAgentsHarness().run(_PROMPT)
    assert result.has_errors()
    assert "AGENT_TRANSPORT" in result.errors[0]


def test_the_delegation_seam_settles_at_once_without_card_ids(
    fake_bus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No activity artifact, no card ids: the wait finds nothing outstanding.

    The seam is wired (a status turn would be a follow-up task on the same
    context) but has nothing to do until an executor publishes its tool
    trace; the run must not sleep a poll interval or exec into the pod.
    """
    monkeypatch.setenv("AGENT_DELEGATION_TIMEOUT", "1800")
    monkeypatch.setenv("AGENT_DELEGATION_POLL_INTERVAL", "30")
    shells: list[str] = []
    monkeypatch.setattr(
        harness, "_agent_shell", lambda script, timeout: shells.append(script) or ""
    )
    started = time.monotonic()
    result = KubeAgentsHarness().run(_PROMPT)

    assert not result.has_errors()
    assert result.output == _ANSWER
    assert time.monotonic() - started < 5
    assert len(fake_bus.instances) == 1
    assert shells == []
    assert result.metadata["worker_commands"] is None


def test_a_status_turn_is_a_follow_up_task_on_the_same_context(
    fake_bus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a card id in the activity trace, the wait polls over the bus."""
    monkeypatch.setenv("AGENT_DELEGATION_TIMEOUT", "1800")
    monkeypatch.setenv("AGENT_DELEGATION_POLL_INTERVAL", "0")
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "")
    filed = _artifact(
        "t",
        "activity",
        [
            {
                "kind": "data",
                "data": {
                    "name": "kanban_create",
                    "args": {},
                    "result": '{"task_id": "card-7"}',
                },
            }
        ],
    )
    settled = _artifact(
        "t",
        "activity",
        [
            {
                "kind": "data",
                "data": {
                    "name": "kanban_show",
                    "args": {"task_id": "card-7"},
                    "result": '{"task": {"id": "card-7", "status": "done", "result": "RCA: OOMKilled"}}',
                },
            }
        ],
    )
    fake_bus.script = [
        [
            _status("t", "submitted"),
            filed,
            _artifact("t", "result", [{"kind": "text", "text": "Filed card-7."}]),
            _status("t", "completed", final=True),
        ],
        [
            _status("t", "submitted"),
            settled,
            _artifact("t", "result", [{"kind": "text", "text": "card-7 is done."}]),
            _status("t", "completed", final=True),
        ],
    ]
    result = KubeAgentsHarness().run(_PROMPT)

    assert not result.has_errors(), result.errors
    first, follow = fake_bus.instances
    assert follow.ids.context_id == first.ids.context_id
    assert follow.ids.correlation_id == first.ids.correlation_id
    assert follow.ids.task_id != first.ids.task_id
    assert "card-7" in follow.prompt and "kanban_show" in follow.prompt
    assert "RCA: OOMKilled" in result.output
    # The delegating turn's closer stays the final message; the poll's does not
    # overwrite it (see _fold_status_turn), but the delivered result joins it.
    assert result.metadata["final_message"].startswith("Filed card-7.")
    assert "RCA: OOMKilled" in result.metadata["final_message"]


# --------------------------------------------------------------------------
# The port-forward target and the credential read
# --------------------------------------------------------------------------


def test_the_nats_forward_names_the_service_and_client_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_NAMESPACE", "kubeagents-system")
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "ctx")
    cmd = harness._port_forward_command(24222, "platform-agent-a2a-nats", 4222)
    assert cmd == [
        "kubectl",
        "port-forward",
        "svc/platform-agent-a2a-nats",
        "24222:4222",
        "-n",
        "kubeagents-system",
        "--context",
        "ctx",
    ]
    # The api path's command is byte-for-byte what it was.
    assert harness._port_forward_command(8642) == [
        "kubectl",
        "port-forward",
        "svc/platform-agent",
        "8642:8642",
        "-n",
        "kubeagents-system",
        "--context",
        "ctx",
    ]


def test_the_credential_comes_from_the_creds_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    import subprocess

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=base64.b64encode(b"s3cret").decode(), stderr=""
        )

    monkeypatch.setattr(harness.subprocess, "run", _run)
    monkeypatch.delenv("AGENT_A2A_NATS_PASSWORD", raising=False)
    monkeypatch.setenv("AGENT_NAMESPACE", "kubeagents-system")
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "ctx")

    assert harness._a2a_password("platform-agent-a2a-nats") == "s3cret"
    (cmd,) = calls
    assert cmd[:4] == ["kubectl", "get", "secret", "platform-agent-a2a-nats-creds"]
    assert "-n" in cmd and "kubeagents-system" in cmd and "--context" in cmd and "ctx" in cmd
    assert cmd[-1] == "jsonpath={.data.gateway-password}"


def test_a_missing_creds_secret_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr='Error from server (NotFound): secrets "platform-agent-a2a-nats-creds" not found',
        )

    monkeypatch.setattr(harness.subprocess, "run", _run)
    monkeypatch.delenv("AGENT_A2A_NATS_PASSWORD", raising=False)
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "ctx")
    with pytest.raises(RuntimeError, match="NotFound"):
        harness._a2a_password("platform-agent-a2a-nats")


# --------------------------------------------------------------------------
# The scorer reads an a2a record as a real run
# --------------------------------------------------------------------------


def test_the_scorer_and_the_transport_agree_on_the_status_event_name() -> None:
    assert scoring.A2A_STATUS_EVENT == a2a.EVENT_ENTRY_STATUS


def test_an_a2a_record_passes_rung_3_without_tokens(kanban_task, make_run) -> None:
    """Null tokens plus a terminal a2a event is a real run, not a skeleton."""

    def as_a2a(rec: dict[str, Any]) -> None:
        fold = a2a.Fold("task-1")
        for env in _lifecycle("task-1"):
            fold.apply(env)
        rec["trajectory"] = list(fold.trajectory)
        rec["tools"] = [e["name"] for e in fold.trajectory]
        rec["tokens"] = dict.fromkeys(
            ("input", "cached", "cache_write", "reasoning", "output", "total")
        )

    verdict = grade_case(load_case(kanban_task), [make_run(mutate=as_a2a)], admitted=False)
    assert verdict.rung is Rung.GREEN, verdict.reason
    assert verdict.reps[0].outcome == "pass"


def test_an_a2a_record_without_a_terminal_is_still_not_a_run(kanban_task, make_run) -> None:
    """A submitted-but-never-finished task carries no evidence a model ran."""

    def unfinished(rec: dict[str, Any]) -> None:
        fold = a2a.Fold("task-1")
        fold.apply(_status("task-1", "submitted"))
        fold.apply(_status("task-1", "working"))
        rec["trajectory"] = list(fold.trajectory)
        rec["tokens"] = dict.fromkeys(
            ("input", "cached", "cache_write", "reasoning", "output", "total")
        )

    verdict = grade_case(load_case(kanban_task), [make_run(mutate=unfinished)], admitted=False)
    assert verdict.rung is Rung.NOT_A_REAL_RUN
    assert "tokens.total is null" in verdict.reason
