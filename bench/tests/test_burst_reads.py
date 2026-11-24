"""The burst-reads measurement (burst_reads): its directive, its precondition,
its record and grading, and the harness's run of it over the inject door."""

from __future__ import annotations

import threading
from typing import Any, ClassVar

import pytest
from kube_agents_bench import burst_reads, harness
from kube_agents_bench import inject_transport as inject

DIRECTIVE = burst_reads.DIRECTIVE
ASK = "/session For every cluster in the fleet, how many nodes and which pods are not running?"


def entry(seq: int, kind: str, **fields: Any) -> dict[str, Any]:
    return {"seq": seq, "kind": kind, "ts": "2026-11-23T00:00:00Z", **fields}


# --- the directive ---------------------------------------------------------


def test_an_ordinary_ask_is_not_a_burst_reads_ask():
    assert burst_reads.parse(ASK) is None
    assert burst_reads.parse(f"check\n{DIRECTIVE}") is None


def test_the_directive_strips_off_and_defaults():
    got = burst_reads.parse(f"{DIRECTIVE}\n{ASK}")
    assert got == burst_reads.BurstAsk(prompt=ASK, levels=(1, 3, 6))


def test_the_levels_can_be_named():
    assert burst_reads.parse(f"{DIRECTIVE} levels=2,4\n{ASK}").levels == (2, 4)
    assert burst_reads.parse(f"{DIRECTIVE} levels=1\n{ASK}").levels == (1,)


@pytest.mark.parametrize(
    "first",
    [
        f"{DIRECTIVE}levels=1,3",
        f"{DIRECTIVE} levels=",
        f"{DIRECTIVE} levels=0,3",
        f"{DIRECTIVE} levels=3,1",
        f"{DIRECTIVE} levels=3,3",
        f"{DIRECTIVE} levels=1,,3",
        f"{DIRECTIVE} levels=1,{burst_reads.MAX_LEVEL + 1}",
        f"{DIRECTIVE} levels=one",
        f"{DIRECTIVE} levels=1 levels=3",
        f"{DIRECTIVE} hold=30",
    ],
)
def test_a_bad_directive_is_refused(first):
    with pytest.raises(ValueError):
        burst_reads.parse(f"{first}\n{ASK}")


def test_the_ask_must_open_a_session():
    with pytest.raises(ValueError, match="/session"):
        burst_reads.parse(f"{DIRECTIVE}\nhow many nodes per cluster?")
    with pytest.raises(ValueError, match="/session"):
        burst_reads.parse(f"{DIRECTIVE}\n/session off")
    with pytest.raises(ValueError):
        burst_reads.parse(f"{DIRECTIVE}\n  ")


def test_each_conversation_has_its_own_key_and_message_id():
    keys = {burst_reads.conversation_key("run/case/1", lv, i) for lv in (1, 3, 6) for i in range(lv)}
    assert len(keys) == 10 and "run/case/1-L3-2" in keys
    assert burst_reads.message_id("m", 6, 5) == "m-L6-5"


# --- the precondition ------------------------------------------------------


def test_a_cap_at_or_above_the_highest_level_can_run_it():
    assert burst_reads.precondition(6, (1, 3, 6)) == ""
    assert burst_reads.precondition(10, (1, 3, 6)) == ""


def test_a_low_or_unread_cap_cannot():
    why = burst_reads.precondition(4, (1, 3, 6))
    assert "level 6" in why and "cap is 4" in why
    assert "could not be read" in burst_reads.precondition(None, (1,))


# --- the record and its grade ----------------------------------------------


def _level(level: int, states: list[str], wall: float = 10.0) -> burst_reads.LevelRecord:
    record = burst_reads.LevelRecord(level=level, started_at="s", ended_at="e", wall_seconds=wall)
    for i, state in enumerate(states):
        record.conversations.append(
            burst_reads.ConversationRecord(level=level, index=i, conversation=f"c-L{level}-{i}", task_id=f"t{i}", terminal_state=state)
        )
    return record


def test_every_conversation_completed_passes():
    levels = [_level(1, ["completed"]), _level(3, ["completed"] * 3, 42.4)]
    got = burst_reads.assess(levels, (1, 3))
    assert got["name"] == burst_reads.ENTRY and got["status"] == "completed", got
    assert got["args"]["levels"][1]["completed"] == 3
    assert got["args"]["levels"][1]["conversations"][2]["conversation"] == "c-L3-2"
    assert "level 3: 3 of 3 conversations completed (completed 3) in 42s" in burst_reads.summary(levels)


def test_a_conversation_that_did_not_complete_fails():
    got = burst_reads.assess([_level(1, ["completed"]), _level(3, ["completed", "canceled", "completed"])], (1, 3))
    assert got["status"] == "error" and "level 3 conversation 1" in got["result"] and "canceled" in got["result"]


def test_a_level_that_did_not_run_fails():
    got = burst_reads.assess([_level(1, ["completed"])], (1, 3))
    assert got["status"] == "error" and "planned" in got["result"]


def test_infrastructure_names_the_first_conversation():
    levels = [_level(1, ["completed"]), _level(3, ["", "", ""])]
    levels[1].conversations[1].infrastructure = "queued"
    assert burst_reads.infrastructure(levels) == "level 3, conversation c-L3-1: queued"
    assert burst_reads.infrastructure([_level(1, ["completed"])]) == ""


def test_stamps_are_utc_with_a_z():
    got = burst_reads.utc_stamp()
    assert got.endswith("Z") and "T" in got


# --- the harness's run ------------------------------------------------------


def test_the_api_transport_refuses_a_burst_reads_ask(monkeypatch):
    monkeypatch.setenv("AGENT_TRANSPORT", "api")
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert DIRECTIVE in " ".join(got.errors)


def test_the_directive_is_known_to_the_harness():
    assert harness._unknown_directive(f"{DIRECTIVE} levels=1\n{ASK}") == ""


def _inject_env(monkeypatch, cap: str | None):
    monkeypatch.setenv("AGENT_TRANSPORT", "inject")
    monkeypatch.setenv("AGENT_INJECT_TOKEN", "tok")
    monkeypatch.setenv("AGENT_INJECT_URL", "http://door")
    monkeypatch.setattr(harness, "_kubectl_read", lambda *a, **k: cap)


@pytest.mark.parametrize("cap", ["4", None, "two"])
def test_a_cap_below_the_highest_level_is_infrastructure_before_anything_is_sent(monkeypatch, cap):
    _inject_env(monkeypatch, cap)
    sent: list[Any] = []
    monkeypatch.setattr(inject, "_request", lambda *a, **k: sent.append(a) or {})
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\n{ASK}")
    assert got.errors and got.errors[0].startswith(harness.INFRA_FAILURE_MARKER), got.errors
    assert sent == []


_RealInjectTask = inject.InjectTask


class _FakeTask(_RealInjectTask):
    """InjectTask with its network scripted. Every instance is kept, and
    every submit, terminal and cancel is logged in order, for the
    assertions. ``plan`` maps a conversation's ``-L<level>-<index>`` suffix
    to what it does: ``complete`` (default), ``deadline`` (still working at
    the deadline), ``queued`` and ``refused`` (at submit), ``never-started``,
    ``door-down`` (the follow loses the door) and ``boom`` (an unexpected
    error while following)."""

    made: ClassVar[list[_FakeTask]] = []
    log: ClassVar[list[tuple[str, str]]] = []
    plan: ClassVar[dict[str, str]] = {}
    cancel_publishes: ClassVar[bool] = True
    lock = threading.Lock()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cancels: list[str] = []
        self.left = 0
        self.suffix = self.conversation.rsplit("-", 2)[-2] + "-" + self.conversation.rsplit("-", 1)[-1]
        with _FakeTask.lock:
            _FakeTask.made.append(self)

    def _note(self, what: str) -> None:
        with _FakeTask.lock:
            _FakeTask.log.append((what, self.suffix))

    @property
    def what(self) -> str:
        return _FakeTask.plan.get(self.suffix, "complete")

    def preflight(self):
        self.first_event_grace = 60.0

    def submit(self):
        self._note("submit")
        if self.what == "queued":
            self.refusal = inject.REFUSAL_QUEUED
            return ""
        if self.what == "refused":
            self.refusal = inject.REFUSAL_NO_TASK
            return ""
        self.conversation = "inject:" + self.conversation
        self.task_id = f"task-{self.suffix}"
        return self.task_id

    def await_terminal(self, task_id, *, deadline, on_poll=None):
        fold = inject.Fold(task_id)
        fold.apply(entry(1, "task", taskId=task_id))
        if self.what == "door-down":
            raise inject.InjectUnavailable("connection refused", retryable=False)
        if self.what == "boom":
            raise RuntimeError("bug")
        if self.what == "never-started":
            return inject.Exchange(fold, inject.OUTCOME_NEVER_STARTED, self.conversation, task_id)
        fold.note_executor_state(inject.STATE_WORKING)
        if self.what == "deadline":
            return inject.Exchange(fold, inject.OUTCOME_DEADLINE, self.conversation, task_id)
        fold.apply(entry(2, "post", text="a-L table", messageId="inj-2"))
        fold.apply(entry(3, "terminal", taskId=task_id, state="completed", source="executor"))
        self._note("terminal")
        return inject.Exchange(fold, inject.OUTCOME_TERMINAL, self.conversation, task_id)

    def cancel(self, task_id="", *, settle=None):
        self._note("cancel")
        self.cancels.append(task_id)
        self.cancel_sent = bool(task_id) and _FakeTask.cancel_publishes

    def leave_line(self):
        self.left += 1
        return True

    def released(self, task_id):
        # A cancelled straggler's session is released at once, unless the
        # plan says it sticks.
        self._note("released")
        return self.what != "stuck"


@pytest.fixture
def fake(monkeypatch):
    _FakeTask.made = []
    _FakeTask.log = []
    _FakeTask.plan = {}
    _FakeTask.cancel_publishes = True
    _inject_env(monkeypatch, "10")
    monkeypatch.setattr(inject, "InjectTask", _FakeTask)
    return _FakeTask


def _run(levels: str = "1,3,6"):
    return harness.KubeAgentsHarness()._execute(f"{DIRECTIVE} levels={levels}\n{ASK}")


def _infra(result) -> str:
    assert result.errors and result.errors[0].startswith(harness.INFRA_FAILURE_MARKER), result.errors
    return result.errors[0]


def _entry(result) -> dict[str, Any]:
    return next(e for e in result.trajectory if e["name"] == burst_reads.ENTRY)


def _sent(fake) -> list[_FakeTask]:
    return [t for t in fake.made if any(w == "submit" and s == t.suffix for w, s in fake.log)]


def test_every_level_runs_with_distinct_conversations_and_passes(fake):
    got = _run()
    assert not got.errors, got.errors
    record = _entry(got)
    assert record["status"] == "completed"
    assert [lv["level"] for lv in record["args"]["levels"]] == [1, 3, 6]
    convs = [c for lv in record["args"]["levels"] for c in lv["conversations"]]
    assert len(convs) == 10 and len({c["conversation"] for c in convs}) == 10
    assert len({t.message_id for t in _sent(fake)}) == 10
    assert all(c["terminal_state"] == "completed" and c["submitted_at"].endswith("Z") and c["ended_at"] for c in convs)
    assert convs[-1]["conversation"].startswith("inject:") and convs[-1]["conversation"].endswith("-L6-5")
    assert all(lv["started_at"] and lv["ended_at"] and lv["wall_seconds"] is not None for lv in record["args"]["levels"])
    assert got.metadata["burst_reads"] == record["args"]
    assert "level 6: 6 of 6 conversations completed" in got.output
    # Each conversation's lifecycle is on the record, tagged with its key.
    tagged = {e["args"]["conversation"] for e in got.trajectory if e["name"] == inject.EVENT_ENTRY_STATUS}
    assert len(tagged) == 10
    assert all(not t.cancels for t in fake.made), "a task that ended is not cancelled"


def test_levels_do_not_overlap(fake):
    _run()
    def level_of(suffix: str) -> int:
        return int(suffix.split("-")[0][1:])

    seen_ended: dict[int, int] = {}
    for what, suffix in fake.log:
        level = level_of(suffix)
        if what == "submit":
            assert all(seen_ended.get(lv, 0) == lv for lv in (1, 3, 6) if lv < level), fake.log
        if what == "terminal":
            seen_ended[level] = seen_ended.get(level, 0) + 1


def test_a_straggler_is_cancelled_and_fails_the_run(fake):
    fake.plan = {"L3-1": "deadline"}
    got = _run()
    assert not got.errors[0].startswith(harness.INFRA_FAILURE_MARKER), got.errors
    record = _entry(got)
    assert record["status"] == "error" and "level 3 conversation 1" in record["result"]
    straggler = next(t for t in fake.made if t.suffix == "L3-1")
    assert straggler.cancels == ["task-L3-1"]
    conv = record["args"]["levels"][1]["conversations"][1]
    assert conv["cancelled"] and conv["cancel_published"]
    # The next level still ran, after the straggler was cancelled.
    assert fake.log.index(("cancel", "L3-1")) < fake.log.index(("submit", "L6-0"))


def test_a_straggler_whose_cancel_was_not_published_is_infrastructure(fake):
    fake.plan = {"L3-1": "deadline"}
    fake.cancel_publishes = False
    got = _run()
    assert "would overlap" in _infra(got)
    assert not any(s.startswith("L6") for _, s in fake.log), "the next level would overlap the straggler"


@pytest.mark.parametrize(("what", "why"), [("queued", "queued it at the session cap"), ("refused", "started no task")])
def test_a_conversation_queued_or_refused_at_submit_is_infrastructure(fake, what, why):
    fake.plan = {"L3-2": what}
    got = _run()
    detail = _infra(got)
    assert why in detail and "L3-2" in detail
    assert not any(s.startswith("L6") for _, s in fake.log), "the run stops at the level"
    # The record still reaches the results file, through the trajectory.
    conv = _entry(got)["args"]["levels"][1]["conversations"][2]
    assert conv[what] is True
    if what == "queued":
        assert next(t for t in fake.made if t.suffix == "L3-2").left == 1


def test_a_conversation_no_executor_took_is_infrastructure(fake):
    fake.plan = {"L1-0": "never-started"}
    assert "no executor took" in _infra(_run())


def test_a_door_outage_is_infrastructure_and_the_task_is_cancelled(fake):
    fake.plan = {"L3-0": "door-down"}
    got = _run()
    assert "could not be reached" in _infra(got)
    assert next(t for t in fake.made if t.suffix == "L3-0").cancels == ["task-L3-0"]


def test_anything_running_is_cancelled_when_the_run_breaks(fake):
    fake.plan = {"L3-0": "boom"}
    with pytest.raises(RuntimeError):
        _run()
    assert next(t for t in fake.made if t.suffix == "L3-0").cancels == ["task-L3-0"]
    assert all(not t.cancels for t in fake.made if t.suffix != "L3-0")


def test_a_sub_floor_budget_is_refused_before_anything_is_sent(fake, monkeypatch):
    monkeypatch.setenv("AGENT_INJECT_TIMEOUT", "5")
    got = _run()
    assert "AGENT_INJECT_TIMEOUT" in " ".join(got.errors)
    assert fake.log == []


def test_the_entry_is_graded_on_every_record():
    """The harness writes the entry itself, so a door that showed no tool
    calls says nothing about it (cases.HARNESS_WRITTEN_ENTRIES)."""
    from kube_agents_bench import cases

    assert burst_reads.ENTRY in cases.HARNESS_WRITTEN_ENTRIES


def test_the_next_level_waits_for_a_cancelled_straggler_to_be_released(fake):
    fake.plan = {"L3-1": "deadline"}
    _run()
    assert fake.log.index(("released", "L3-1")) < fake.log.index(("submit", "L6-0"))


def test_a_straggler_never_released_is_infrastructure(fake, monkeypatch):
    monkeypatch.setattr(burst_reads, "STRAGGLER_RELEASE_SECONDS", 0.0)
    fake.plan = {"L3-1": "stuck"}
    original = _FakeTask.await_terminal

    def deadline_then_stuck(self, task_id, *, deadline, on_poll=None):
        if self.suffix == "L3-1":
            fold = inject.Fold(task_id)
            fold.apply(entry(1, "task", taskId=task_id))
            fold.note_executor_state(inject.STATE_WORKING)
            return inject.Exchange(fold, inject.OUTCOME_DEADLINE, self.conversation, task_id)
        return original(self, task_id, deadline=deadline, on_poll=on_poll)

    monkeypatch.setattr(_FakeTask, "await_terminal", deadline_then_stuck)
    why = _infra(_run())
    assert "still running" in why and "after its cancel" in why
    assert not any(what == "submit" and suffix.startswith("L6") for what, suffix in fake.log)


def test_a_conversation_past_the_deadline_fails_even_if_it_then_completed():
    level = burst_reads.LevelRecord(level=1)
    level.conversations.append(
        burst_reads.ConversationRecord(
            level=1, index=0, conversation="c", task_id="t", terminal_state=burst_reads.STATE_COMPLETED, timed_out=True
        )
    )
    got = burst_reads.assess([level], [1])
    assert got["status"] == "error" and "still running at the deadline" in got["result"]
    level.wall_seconds = 1.0
    assert level.completed() == 0
    assert "0 of 1 conversations completed" in burst_reads.summary([level])


def test_the_result_sums_every_conversations_tokens(fake, monkeypatch):
    original = _FakeTask.await_terminal

    def with_usage(self, task_id, *, deadline, on_poll=None):
        exchange = original(self, task_id, deadline=deadline, on_poll=on_poll)
        exchange.fold.usage = {"input": 10, "output": 1}
        return exchange

    monkeypatch.setattr(_FakeTask, "await_terminal", with_usage)
    got = _run("1,3")
    single = harness._inject_tokens({"input": 10, "output": 1})
    for bucket, value in single.items():
        if isinstance(value, int) and not isinstance(value, bool) and value:
            assert got.tokens[bucket] == 4 * value, (bucket, got.tokens)



@pytest.mark.parametrize(
    "body, released",
    [
        ({}, False),
        ({"probe": {"active": True, "taskId": "t1", "error": "kv: timeout"}}, False),
        ({"probe": {"active": True, "taskId": "t1"}}, False),
        ({"probe": {"active": True, "taskId": "t1", "final": True}}, True),
        ({"probe": {"active": True, "taskId": "t2"}}, True),
        ({"probe": {"active": False}}, True),
    ],
)
def test_released_counts_only_a_read_that_looked_and_saw_it_gone(monkeypatch, body, released):
    """A read that couldn't look is not released, so the next level waits."""
    task = inject.InjectTask(base_url="http://x", conversation="c", prompt="p", token="t", author="a", message_id="m")
    monkeypatch.setattr(task, "_poll", lambda task_id, after, wait, **kw: body)
    assert task.released("t1") is released
