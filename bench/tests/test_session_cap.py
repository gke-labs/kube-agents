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
    assert got == session_cap.CapAsk(prompt=ASK, max_cap=2, hold_seconds=session_cap.DEFAULT_HOLD_SECONDS)


def test_the_hold_outlasts_the_fillers_wait():
    """Filler 0 must still be busy when the last filler starts: the default
    hold is the shared filler wait plus a margin, and any hold's wait is the
    hold less that margin."""
    assert session_cap.DEFAULT_HOLD_SECONDS == session_cap.FILLER_WAIT_SECONDS + session_cap.HOLD_MARGIN_SECONDS
    assert session_cap.filler_wait(session_cap.DEFAULT_HOLD_SECONDS) == session_cap.FILLER_WAIT_SECONDS
    assert session_cap.filler_wait(session_cap.MIN_HOLD_SECONDS) > 0


def test_the_fields_can_be_named():
    got = session_cap.parse(f"{DIRECTIVE} max-cap=3 hold=300\n{ASK}")
    assert (got.max_cap, got.hold_seconds) == (3, 300)


@pytest.mark.parametrize(
    "first",
    [
        f"{DIRECTIVE}max-cap=2",
        f"{DIRECTIVE} max-cap=0",
        f"{DIRECTIVE} max-cap=9",
        f"{DIRECTIVE} hold=5",
        f"{DIRECTIVE} hold=60",
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


_RealInjectTask = inject.InjectTask
RESTART = "🚦 not started: the gateway restarted while this conversation waited for a session worker — send it again"
COUNT_FAILED = "⚠️ not started: can't count the running session workers right now — try again in a moment"


class _FakeTask(_RealInjectTask):
    """InjectTask with its network scripted: fillers start and work at once;
    the ask does what the scenario says. Every instance is kept, for the
    assertions. The rest (refusal_text, ...) is the real one's.

    Scenarios: ``queued`` (the ask is queued, then started and answered),
    ``refused`` (the POST answered with the cap's refusal), ``other-refusal``
    (the POST answered with another "not started"), ``line-refused`` and
    ``restart`` (queued, then the gateway's refusal or restart post),
    ``line-timeout`` (queued, never started), ``line-drop`` (the door goes
    away while the queued turn is followed), ``started`` (the ask started at
    once), ``never-started`` and ``deadline`` (the queued turn's task ends so).
    Knobs: ``filler_works``, ``filler_queued``, ``filler_running`` (the read
    right before the ask), ``cancel_publishes``."""

    made: ClassVar[list[_FakeTask]] = []
    scenario: ClassVar[str] = "queued"
    filler_works: ClassVar[bool] = True
    filler_queued: ClassVar[bool] = False
    filler_running: ClassVar[bool] = True
    cancel_publishes: ClassVar[bool] = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cancels: list[str] = []
        self.left: int = 0
        self.filler = "-fill-" in self.conversation
        _FakeTask.made.append(self)

    def preflight(self):
        self.first_event_grace = 60.0

    def submit(self):
        if self.filler:
            if _FakeTask.filler_queued:
                self.refusal = inject.REFUSAL_QUEUED
                return ""
            self.task_id = f"fill-task-{len(_FakeTask.made)}"
        elif _FakeTask.scenario == "started":
            self.task_id = "ask-task"
        elif _FakeTask.scenario in ("refused", "other-refusal"):
            self.refusal = inject.REFUSAL_NO_TASK
        else:
            self.refusal = inject.REFUSAL_QUEUED
        return self.task_id

    def cancel(self, task_id="", *, settle=None):
        self.cancels.append(task_id)
        self.cancel_sent = bool(task_id) and (_FakeTask.cancel_publishes or not self.filler)

    def leave_line(self):
        self.left += 1
        self.left_line = True
        return True

    def still_running(self, task_id):
        return _FakeTask.filler_running

    def _poll(self, task_id, after, budget, *, probe=True):
        if _FakeTask.scenario == "other-refusal":
            return {"entries": [entry(1, "post", text=COUNT_FAILED)]}
        return {"entries": [entry(1, "post", text=REFUSAL)]}

    def await_working(self, task_id, deadline):
        fold = inject.Fold(task_id)
        fold.note_executor_state(inject.STATE_SUBMITTED)
        if _FakeTask.filler_works:
            fold.note_executor_state(inject.STATE_WORKING)
        return fold

    def await_queued_start(self, deadline):
        assert all(f.cancels for f in _FakeTask.made if f.filler), "the fillers were not released first"
        if _FakeTask.scenario == "line-refused":
            self.line_refusal = REFUSAL
            return ""
        if _FakeTask.scenario == "restart":
            self.line_refusal = RESTART
            return ""
        if _FakeTask.scenario == "line-timeout":
            return ""
        if _FakeTask.scenario == "line-drop":
            raise inject.InjectUnavailable("connection refused", retryable=False)
        self.task_id = "ask-task"
        return self.task_id

    def await_terminal(self, task_id, *, deadline, on_poll=None):
        fold = inject.Fold(task_id)
        fold.apply(entry(1, "post", text=NOTICE, messageId="inj-1"))
        fold.apply(entry(2, "task", taskId=task_id))
        if _FakeTask.scenario == "never-started":
            return inject.Exchange(fold, inject.OUTCOME_NEVER_STARTED, self.conversation, task_id)
        if _FakeTask.scenario == "deadline":
            fold.note_executor_state(inject.STATE_WORKING)
            fold.apply(entry(3, "post", text="still working", messageId="inj-2"))
            return inject.Exchange(fold, inject.OUTCOME_DEADLINE, self.conversation, task_id)
        fold.apply(entry(3, "post", text="PINEAPPLE", messageId="inj-2"))
        fold.apply(entry(4, "terminal", taskId=task_id, state="completed", source="executor"))
        return inject.Exchange(fold, inject.OUTCOME_TERMINAL, self.conversation, task_id)


def _reset_fake():
    _FakeTask.made = []
    _FakeTask.scenario = "queued"
    _FakeTask.filler_works = True
    _FakeTask.filler_queued = False
    _FakeTask.filler_running = True
    _FakeTask.cancel_publishes = True


@pytest.fixture
def fake_tasks(monkeypatch):
    _reset_fake()
    _inject_env(monkeypatch, "2")
    monkeypatch.setattr(inject, "InjectTask", _FakeTask)
    return _FakeTask


def _run():
    return harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")


def _infra(result) -> str:
    assert result.errors and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER), result.errors
    return result.errors[0]


def _queued_entry(result):
    return next(e for e in result.trajectory if e["name"] == session_cap.QUEUED_ENTRY)


def _ask(fake_tasks):
    return next(t for t in fake_tasks.made if not t.filler)


def _fillers(fake_tasks):
    return [t for t in fake_tasks.made if t.filler]


def test_a_queued_ask_is_answered_and_the_fillers_released(fake_tasks):
    got = _run()
    assert not got.errors, got.errors
    assert _queued_entry(got)["status"] == "completed", _queued_entry(got)
    assert "PINEAPPLE" in got.output
    assert len(_fillers(fake_tasks)) == 2 and all(len(t.cancels) == 1 for t in _fillers(fake_tasks))
    assert _ask(fake_tasks).cancels == [], "a task that ended is not cancelled"


def test_every_graded_record_opens_with_the_fillers_lifecycle(fake_tasks):
    got = _run()
    working = [e for e in got.trajectory if e["name"] == inject.EVENT_ENTRY_STATUS and e["args"].get("conversation")]
    assert {e["args"]["conversation"] for e in working} == {t.conversation for t in _fillers(fake_tasks)}
    assert any(e["args"]["state"] == inject.STATE_WORKING for e in working)


def test_a_sub_floor_budget_is_refused_before_anything_is_sent(fake_tasks, monkeypatch):
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "5")
    got = _run()
    assert "AGENT_INJECT_TIMEOUT" in " ".join(got.errors)
    assert _fillers(fake_tasks) == []


def test_a_queued_filler_is_infrastructure_before_the_ask_and_leaves_the_line(fake_tasks):
    fake_tasks.filler_queued = True
    reason = _infra(_run())
    assert "was queued" in reason and "the ask was not sent" in reason
    assert _ask(fake_tasks).refusal == ""  # built, never submitted
    assert _fillers(fake_tasks)[0].left == 1


def test_a_filler_that_never_works_is_infrastructure_before_the_ask(fake_tasks):
    fake_tasks.filler_works = False
    assert "was not working" in _infra(_run())
    assert _ask(fake_tasks).refusal == ""
    assert all(t.cancels for t in _fillers(fake_tasks))


def test_a_filler_no_longer_running_before_the_ask_is_infrastructure(fake_tasks):
    fake_tasks.filler_running = False
    reason = _infra(_run())
    assert "no longer full" in reason
    assert _ask(fake_tasks).refusal == ""


def test_an_ask_task_no_executor_took_is_cancelled_and_infrastructure(fake_tasks):
    fake_tasks.scenario = "never-started"
    assert "no executor took task ask-task" in _infra(_run())
    assert _ask(fake_tasks).cancels == ["ask-task"]


def test_an_ask_task_at_its_budget_is_cancelled_and_graded_as_the_timeout(fake_tasks):
    fake_tasks.scenario = "deadline"
    got = _run()
    assert not (got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)), got.errors
    assert any("did not reach a terminal state" in e for e in got.errors)
    assert _ask(fake_tasks).cancels == ["ask-task"]


def test_an_ask_refused_at_the_cap_is_graded_red(fake_tasks):
    fake_tasks.scenario = "refused"
    got = _run()
    assert not got.errors
    assert got.output == REFUSAL
    assert _queued_entry(got)["status"] == "error"
    assert any(e["name"] == inject.EVENT_ENTRY_POST and e["result"] == REFUSAL for e in got.trajectory)
    assert all(t.cancels for t in _fillers(fake_tasks))


def test_another_not_started_at_the_post_is_infrastructure(fake_tasks):
    fake_tasks.scenario = "other-refusal"
    assert "not with the cap's refusal" in _infra(_run())


@pytest.mark.parametrize(("scenario", "words"), [("line-refused", REFUSAL), ("restart", RESTART)])
def test_a_refusal_after_the_queue_is_infrastructure_in_the_gateways_words(fake_tasks, scenario, words):
    fake_tasks.scenario = scenario
    reason = _infra(_run())
    assert words in reason and "line's bound" not in reason
    assert _ask(fake_tasks).left == 1


def test_a_queued_turn_that_never_started_is_infrastructure_naming_the_bound_and_leaves_the_line(fake_tasks):
    fake_tasks.scenario = "line-timeout"
    assert "did not start inside the line's bound" in _infra(_run())
    assert _ask(fake_tasks).left == 1


def test_a_door_lost_while_following_the_queued_turn_takes_it_out_of_the_line(fake_tasks):
    fake_tasks.scenario = "line-drop"
    reason = _infra(_run())
    assert "could not be reached" in reason and "taken out of the line" in reason
    assert _ask(fake_tasks).left == 1


def test_a_filler_cancel_not_published_is_retried_and_recorded(fake_tasks):
    fake_tasks.cancel_publishes = False
    got = _run()
    assert all(len(t.cancels) == 2 for t in _fillers(fake_tasks))
    assert got.metadata["fillers_unreleased"] == [t.task_id for t in _fillers(fake_tasks)]
    assert any("may still hold the cap" in e for e in got.errors)


def test_a_filler_cancel_not_published_rides_on_a_later_infrastructure(fake_tasks):
    fake_tasks.cancel_publishes = False
    fake_tasks.scenario = "line-timeout"
    assert "may still hold the cap" in _infra(_run())


def test_an_ask_that_started_at_once_is_infrastructure(fake_tasks):
    fake_tasks.scenario = "started"
    assert "never filled" in _infra(_run())
    assert all(t.cancels for t in _fillers(fake_tasks))


def test_the_waits_retry_through_a_respawned_tunnel(fake_tasks, monkeypatch):
    """The queued wait and the terminal wait go through the inject
    transport's retry: a dropped read is retried, not the end of the run."""
    calls = {"n": 0}
    real = _FakeTask.await_terminal

    def flaky(self, task_id, *, deadline, on_poll=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise inject.InjectUnavailable("dropped")
        return real(self, task_id, deadline=deadline, on_poll=on_poll)

    monkeypatch.setattr(_FakeTask, "await_terminal", flaky)
    monkeypatch.setattr(harness, "_wait_retry_after", lambda exc: None)
    got = _run()
    assert not got.errors, got.errors
    assert calls["n"] == 2


def test_the_session_cap_result_folds_the_worker_total(fake_tasks, monkeypatch):
    folded: list[Any] = []
    monkeypatch.setattr(harness, "_fold_worker_total", lambda tokens: folded.append(tokens))
    _run()
    assert len(folded) == 1


# --- the refusal's wording (InjectTask.refusal_text) -------------------------


def test_a_queued_turn_the_gateway_refused_says_what_the_gateway_said():
    task = _task()
    task.refusal, task.line_refusal = inject.REFUSAL_QUEUED, REFUSAL
    assert REFUSAL in task.refusal_text and "line's bound" not in task.refusal_text


def test_a_queued_turn_that_timed_out_says_the_bound_ran_out():
    task = _task()
    task.refusal = inject.REFUSAL_QUEUED
    assert task.refusal_text == inject.refusal_detail(inject.REFUSAL_QUEUED)
    assert "line's bound" in task.refusal_text


# --- the tunnel ----------------------------------------------------------------


def test_the_session_cap_tunnel_replaces_an_owned_forward_aimed_at_an_old_holder(monkeypatch):
    """The session-cap path forwards through the inject transport's helper,
    so a forward this process opened to an earlier leaseholder is replaced
    rather than reused."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(harness, "_gateway_door_target", lambda *a: "pod/new-holder")
    monkeypatch.setattr(harness, "_owned_forward_target", lambda port: "pod/old-holder")
    monkeypatch.setattr(harness, "_reset_port_forward", lambda port, service, remote_port: calls.append(("reset", service)))
    monkeypatch.setattr(harness, "_ensure_port_forward", lambda port, service, remote_port: calls.append(("ensure", service)))
    harness._forward_inject_door(28099, "platform-agent", "platform-agent-a2a-inject", reset=False)
    assert calls == [("reset", "pod/new-holder")]


def test_the_session_cap_path_uses_the_shared_tunnel(monkeypatch, fake_tasks):
    fake_tasks.scenario = "queued"
    monkeypatch.delenv("AGENT_INJECT_URL")
    used: list[Any] = []
    monkeypatch.setattr(harness, "_forward_inject_door", lambda *a, **k: used.append((a, k)))
    harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert used and used[0][1]["reset"] is False


# --- a directive the harness does not know ---------------------------------


@pytest.mark.parametrize("transport", ["inject", "api"])
def test_an_unknown_directive_is_infrastructure_and_sends_nothing(monkeypatch, transport):
    """A case newer than the grader running it (a pull request is graded with
    its base commit's bench/) must not have its directive sent to the agent as
    text and the run graded: that is how the first evidence runs of this case
    graded a failure on an ask that started at once."""
    _inject_env(monkeypatch, "2")
    monkeypatch.setenv("AGENT_TRANSPORT", transport)
    sent: list[Any] = []
    monkeypatch.setattr(inject, "_request", lambda *a, **k: sent.append(a) or {})
    got = harness.KubeAgentsHarness()._execute(f"[bench:not-a-directive-yet] x=1\n{ASK}")
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "[bench:not-a-directive-yet]" in got.errors[0] and "land on main" in got.errors[0]
    assert sent == []


@pytest.mark.parametrize("typo", ["[bench:session-cap-queu]", "[bench:sesion-cap-queue]", "[bench:reply-under-pushed-reprot]"])
def test_a_typod_directive_is_infrastructure_not_a_model_failure(monkeypatch, typo):
    """A near miss of a real directive is a case-authoring mistake: it must
    surface as infrastructure naming the directive, never reach the agent as
    text and be graded as the model's failure."""
    _inject_env(monkeypatch, "2")
    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    sent: list[Any] = []
    monkeypatch.setattr(inject, "_request", lambda *a, **k: sent.append(a) or {})
    got = harness.KubeAgentsHarness()._execute(f"{typo} max-cap=2\n{ASK}")
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert typo in got.errors[0]
    assert sent == []


def test_every_case_directive_is_one_the_harness_knows():
    import pathlib

    import yaml

    for task in sorted(pathlib.Path(__file__).resolve().parents[1].glob("tasks/*/task.yaml")):
        prompt = str((yaml.safe_load(task.read_text(encoding="utf-8")) or {}).get("prompt") or "")
        assert harness._unknown_directive(prompt) == "", task


def test_an_ordinary_prompt_is_not_an_unknown_directive():
    assert harness._unknown_directive("check the pods") == ""
    assert harness._unknown_directive(f"{DIRECTIVE}\n{ASK}") == ""
    assert harness._unknown_directive("[bench:profile name=cluster]\nask") == ""


# --- the refusal through the scorer -----------------------------------------


def _refusal_trajectory(monkeypatch) -> list[dict[str, Any]]:
    """The trajectory the harness records for an ask refused at the cap."""
    _reset_fake()
    _FakeTask.scenario = "refused"
    _inject_env(monkeypatch, "2")
    monkeypatch.setattr(inject, "InjectTask", _FakeTask)
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert not got.errors and got.output == REFUSAL
    return got.trajectory


def _as_refusal(trajectory):
    def mutate(rec):
        # No usage on an inject record whose ask never ran: the evidence of a
        # run is the executors' lifecycle on the trajectory, not tokens.
        rec["tokens"] = {k: None for k in rec["tokens"]}
        rec["trajectory"] = trajectory
        rec["output"] = REFUSAL
        rec["scores"]["VerificationCorrectness"] = 0.5

    return mutate


def test_a_refusal_at_the_cap_grades_as_a_fail_not_blocked(monkeypatch, kanban_task, make_run):
    from kube_agents_bench import scoring
    from kube_agents_bench.cases import load_case

    spec = load_case(kanban_task)
    run = make_run(mutate=_as_refusal(_refusal_trajectory(monkeypatch)))
    rep = scoring.classify_rep(spec, run, 1)
    assert rep.outcome == "fail", (rep.outcome, rep.reason)
    assert rep.rung is not scoring.Rung.NOT_A_REAL_RUN
    verdict = scoring.grade_case(spec, [run], admitted=False)
    assert verdict.rung is not scoring.Rung.NOT_A_REAL_RUN, verdict.reason
    assert verdict.passes == 0


def test_without_the_fillers_lifecycle_the_refusal_would_be_blocked(monkeypatch, kanban_task, make_run):
    """The control: the fillers' entries are what makes the record a run."""
    from kube_agents_bench import scoring
    from kube_agents_bench.cases import load_case

    bare = [e for e in _refusal_trajectory(monkeypatch) if not e["args"].get("conversation")]
    rep = scoring.classify_rep(load_case(kanban_task), make_run(mutate=_as_refusal(bare)), 1)
    assert rep.outcome == "blocked" and rep.rung is scoring.Rung.NOT_A_REAL_RUN


def test_await_working_reads_until_the_task_works(monkeypatch):
    def probe(state):
        return {"entries": [], "probe": {"active": True, "taskId": "t-1", "executorState": state,
                                         "reachedWorking": state == "working", "graceSeconds": 60}}

    replies = [probe("submitted"), probe("working")]
    monkeypatch.setattr(inject, "_request", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(inject, "POLL_PAUSE_SECONDS", 0)
    fold = _task().await_working("t-1", time.monotonic() + 60)
    assert fold.started and not fold.final
    assert [e["args"]["state"] for e in fold.trajectory] == ["submitted", "working"]


# --- the ordinary inject path's queued turn --------------------------------


def test_a_status_turns_wait_in_line_is_bounded_by_its_budget():
    assert harness._queued_wait(False, 90.0) == 90.0
    assert harness._queued_wait(False, 10_000.0) == inject.QUEUED_START_WAIT_SECONDS
    assert harness._queued_wait(True, 90.0) == inject.QUEUED_START_WAIT_SECONDS


def test_leave_line_cancels_naming_no_task_once(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(inject, "_request", lambda url, timeout, token, payload=None: sent.append((url, payload)) or {})
    task = _task()
    assert task.leave_line() and task.leave_line()
    assert len(sent) == 1
    url, payload = sent[0]
    assert url.endswith(inject.CANCEL_SUFFIX) and "taskId" not in payload
    assert payload["messageId"].endswith(inject.LEAVE_LINE_MESSAGE_ID_SUFFIX)


def test_leave_line_says_when_the_door_did_not_answer(monkeypatch):
    def down(*a, **k):
        raise inject.InjectUnavailable("refused")

    monkeypatch.setattr(inject, "_request", down)
    task = _task()
    assert task.leave_line() is False and task.left_line is False


def test_still_running_reads_the_record(monkeypatch):
    def probe(**fields):
        return {"entries": [], "probe": {"active": True, "taskId": "t-1", "executorState": "working", **fields}}

    for body, want in [(probe(), True), (probe(final=True, executorState="completed"), False), (probe(active=False), False)]:
        monkeypatch.setattr(inject, "_request", lambda *a, _b=body, **k: _b)
        assert _task().still_running("t-1") is want
