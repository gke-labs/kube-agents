"""The session-cap ask (session_cap): its directive, its precondition, its
grading, the harness's run of it, and the inject transport's follow of a
queued turn."""

from __future__ import annotations

import time
from typing import Any, ClassVar

import pytest

from kube_agents_bench import harness, session_cap
from kube_agents_bench import inject_transport as inject

DIRECTIVE = session_cap.DIRECTIVE
ASK = "/session Reply with the single word PINEAPPLE and nothing else."
NOTICE = "⏳ **queued** — every session worker is busy and yours is next in line; I'll start as soon as there's room"
REFUSAL = "🚦 not started: 2 session workers are already running (cap 2). try again later"


def entry(seq: int, kind: str, **fields: Any) -> dict[str, Any]:
    return {"seq": seq, "kind": kind, "ts": "2026-11-23T00:00:00Z", **fields}


# --- the directive ---------------------------------------------------------


def test_an_ordinary_ask_is_not_a_session_cap_ask():
    assert session_cap.parse(ASK) is None
    assert session_cap.parse(f"check\n{DIRECTIVE}") is None


def test_the_directive_strips_off_and_defaults():
    got = session_cap.parse(f"{DIRECTIVE}\n{ASK}")
    assert got == session_cap.CapAsk(prompt=ASK, max_cap=2, hold_seconds=240)


def test_the_fields_can_be_named():
    got = session_cap.parse(f"{DIRECTIVE} max-cap=3 hold=60\n{ASK}")
    assert (got.max_cap, got.hold_seconds) == (3, 60)


@pytest.mark.parametrize(
    "first",
    [
        f"{DIRECTIVE}max-cap=2",
        f"{DIRECTIVE} max-cap=0",
        f"{DIRECTIVE} max-cap=9",
        f"{DIRECTIVE} hold=5",
        f"{DIRECTIVE} hold=soon",
        f"{DIRECTIVE} max-cap=1 max-cap=2",
        f"{DIRECTIVE} later",
    ],
)
def test_a_bad_directive_is_refused(first):
    with pytest.raises(ValueError):
        session_cap.parse(f"{first}\n{ASK}")


def test_the_ask_must_open_a_session():
    with pytest.raises(ValueError, match="/session"):
        session_cap.parse(f"{DIRECTIVE}\nreply with PINEAPPLE")
    with pytest.raises(ValueError):
        session_cap.parse(f"{DIRECTIVE}\n  ")


def test_the_filler_opens_a_session_that_stays_busy():
    got = session_cap.filler_prompt(90)
    assert got.startswith("/session ") and "sleep 90" in got


# --- the precondition ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [(None, None), ("", 10), ("2", 2), (" 3\n", 3), ("0", None), ("two", None)],
)
def test_the_cap_is_read_off_the_gateway_env(raw, want):
    assert session_cap.parse_cap(raw) == want


def test_a_low_cap_can_evaluate_the_case():
    assert session_cap.precondition(2, 2) == ""
    assert session_cap.precondition(1, 2) == ""


def test_a_high_or_unread_cap_cannot():
    why = session_cap.precondition(10, 2)
    assert "at most 2" in why and "is 10" in why and "maxSessions" in why
    assert "could not be read" in session_cap.precondition(None, 2)


# --- the grading -----------------------------------------------------------


def test_the_notice_then_the_task_then_its_answer_passes():
    got = session_cap.assess(
        [
            entry(1, "post", text=NOTICE, messageId="inj-1"),
            entry(2, "edit", text="▶️ **starting** — a session worker is free", messageId="inj-1"),
            entry(3, "task", taskId="t-1"),
            entry(4, "post", text="PINEAPPLE", messageId="inj-2"),
        ],
        "t-1",
    )
    assert got["name"] == session_cap.QUEUED_ENTRY
    assert got["status"] == "completed", got


def test_a_refusal_at_the_cap_fails_with_the_refusal():
    got = session_cap.assess([entry(1, "post", text=REFUSAL)], "")
    assert got["status"] == "error"
    assert "refused the turn at the cap" in got["result"] and "not started" in got["result"]


@pytest.mark.parametrize(
    ("entries", "why"),
    [
        ([entry(1, "task", taskId="t-1"), entry(2, "post", text="PINEAPPLE")], "no queued notice"),
        ([entry(1, "post", text=NOTICE)], "started no task"),
        ([entry(1, "post", text=NOTICE), entry(2, "task", taskId="t-1")], "nothing was posted"),
        ([entry(1, "task", taskId="t-1"), entry(2, "post", text=NOTICE), entry(3, "post", text="x")], "before the queued notice"),
    ],
)
def test_anything_out_of_order_fails(entries, why):
    got = session_cap.assess(entries, "t-1")
    assert got["status"] == "error" and why in got["result"], got


# --- the inject transport's follow of a queued turn -----------------------


def _replies(monkeypatch, replies):
    """Answer each _request with the next reply, recording the URLs asked."""
    asked: list[str] = []

    def request(url, *args, **kwargs):
        asked.append(url)
        return replies.pop(0)

    monkeypatch.setattr(inject, "_request", request)
    return asked


def _task() -> inject.InjectTask:
    return inject.InjectTask(base_url="http://door", conversation="c", prompt=ASK, token="t", message_id="m")


def test_a_queued_post_is_followed_to_the_task_it_starts(monkeypatch):
    _replies(
        monkeypatch,
        [
            {"conversation": "inject:c", "refusal": "queued", "entries": [entry(3, "post", text=NOTICE)]},
            {"entries": [entry(4, "edit", text="▶️ starting", messageId="inj-1")]},
            {"entries": [entry(5, "task", taskId="t-9"), entry(6, "post", text="⏳ working")]},
        ],
    )
    task = _task()
    assert task.submit() == ""
    assert (task.refusal, task.queued_after, task.queued_notice) == (inject.REFUSAL_QUEUED, 2, NOTICE)
    assert task.await_queued_start(time.monotonic() + 60) == "t-9"
    assert task.task_id == "t-9" and task.line_refusal == ""


def test_a_queued_turn_the_gateway_refuses_ends_the_wait(monkeypatch):
    _replies(
        monkeypatch,
        [
            {"conversation": "inject:c", "refusal": "queued", "entries": [entry(1, "post", text=NOTICE)]},
            {"entries": [entry(1, "post", text=NOTICE), entry(2, "post", text=REFUSAL)]},
        ],
    )
    task = _task()
    task.submit()
    task.queued_after = 0
    assert task.await_queued_start(time.monotonic() + 60) == ""
    assert task.line_refusal == REFUSAL


def test_a_deduped_retry_with_no_entries_keeps_the_first_reading(monkeypatch):
    _replies(
        monkeypatch,
        [
            {"conversation": "inject:c", "refusal": "queued", "entries": [entry(4, "post", text=NOTICE)]},
            {"conversation": "inject:c", "refusal": "queued", "entries": [], "deduplicated": True},
        ],
    )
    task = _task()
    task.submit()
    task.submit()
    assert (task.queued_after, task.queued_notice) == (3, NOTICE)


def test_the_wait_ends_at_its_deadline():
    task = _task()
    task.refusal = inject.REFUSAL_QUEUED
    assert task.await_queued_start(0.0) == ""


# --- the harness's run ------------------------------------------------------


def test_the_api_transport_refuses_a_session_cap_ask(monkeypatch):
    monkeypatch.setenv("AGENT_TRANSPORT", "api")
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert DIRECTIVE in " ".join(got.errors)


def _inject_env(monkeypatch, cap: str | None):
    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    monkeypatch.setenv("AGENT_INJECT_TOKEN", "tok")
    monkeypatch.setenv("AGENT_INJECT_URL", "http://door")
    monkeypatch.setattr(harness, "_kubectl_read", lambda *a, **k: cap)


def test_an_install_above_the_cap_is_infrastructure_before_anything_is_sent(monkeypatch):
    _inject_env(monkeypatch, "10")
    sent: list[Any] = []
    monkeypatch.setattr(inject, "_request", lambda *a, **k: sent.append(a) or {})
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "this install's is 10" in got.errors[0]
    assert sent == []


class _FakeTask:
    """InjectTask, scripted: fillers start at once; the ask does what the
    scenario says. Every instance is kept, for the assertions."""

    made: ClassVar[list[_FakeTask]] = []
    scenario: ClassVar[str] = "queued"

    def __init__(self, *, base_url, conversation, prompt, token, author="", message_id=""):
        self.conversation, self.prompt, self.message_id = conversation, prompt, message_id
        self.task_id, self.refusal, self.note, self.line_refusal = "", "", "", ""
        self.cancels: list[str] = []
        self.filler = "-fill-" in conversation
        _FakeTask.made.append(self)

    def preflight(self):
        return None

    def submit(self):
        if self.filler:
            self.task_id = f"fill-task-{len(_FakeTask.made)}"
        elif _FakeTask.scenario == "started":
            self.task_id = "ask-task"
        elif _FakeTask.scenario == "refused":
            self.refusal = inject.REFUSAL_NO_TASK
        else:
            self.refusal = inject.REFUSAL_QUEUED
        return self.task_id

    def cancel(self, task_id="", *, settle=None):
        self.cancels.append(task_id)

    def _poll(self, task_id, after, budget, *, probe=True):
        return {"entries": [entry(1, "post", text=REFUSAL)]}

    def await_queued_start(self, deadline):
        assert all(f.cancels for f in _FakeTask.made if f.filler), "the fillers were not released first"
        self.task_id = "ask-task"
        return self.task_id

    def await_terminal(self, task_id, *, deadline, on_poll=None):
        fold = inject.Fold(task_id)
        for e in [
            entry(1, "post", text=NOTICE, messageId="inj-1"),
            entry(2, "task", taskId=task_id),
            entry(3, "post", text="PINEAPPLE", messageId="inj-2"),
            entry(4, "terminal", taskId=task_id, state="completed", source="executor"),
        ]:
            fold.apply(e)
        return inject.Exchange(fold, inject.OUTCOME_TERMINAL, self.conversation, task_id)


@pytest.fixture
def fake_tasks(monkeypatch):
    _FakeTask.made = []
    _inject_env(monkeypatch, "2")
    monkeypatch.setattr(inject, "InjectTask", _FakeTask)
    return _FakeTask


def test_a_queued_ask_is_answered_and_the_fillers_released(fake_tasks):
    fake_tasks.scenario = "queued"
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert not got.errors, got.errors
    assert got.trajectory[0]["name"] == session_cap.QUEUED_ENTRY
    assert got.trajectory[0]["status"] == "completed", got.trajectory[0]
    assert "PINEAPPLE" in got.output
    fillers = [t for t in fake_tasks.made if t.filler]
    assert len(fillers) == 2 and all(len(t.cancels) == 1 for t in fillers)


def test_an_ask_refused_at_the_cap_is_graded_red(fake_tasks):
    fake_tasks.scenario = "refused"
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert not got.errors
    assert got.output == REFUSAL
    assert got.trajectory[0]["status"] == "error"
    assert all(t.cancels for t in fake_tasks.made if t.filler)


def test_an_ask_that_started_at_once_is_infrastructure(fake_tasks):
    fake_tasks.scenario = "started"
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "never filled" in got.errors[0]
    assert all(t.cancels for t in fake_tasks.made if t.filler)
