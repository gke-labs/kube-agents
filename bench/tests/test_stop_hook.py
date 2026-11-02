"""The stopped ask (stop_hook): its prompt, and the harness's stop after the first card."""

from __future__ import annotations

import pytest
from devops_bench.agents import AgentResult
from kube_agents_bench import board, harness, stop_hook

DIRECTIVE = stop_hook.STOP_DIRECTIVE


def test_an_ordinary_ask_is_not_stopped():
    assert stop_hook.parse("check the pods on x") is None
    assert stop_hook.parse("check\n" + DIRECTIVE) is None


def test_the_directive_strips_off_and_defaults_the_window():
    stop = stop_hook.parse(f"{DIRECTIVE}\nopen a pull request\nthat scales it")
    assert stop == stop_hook.Stop(prompt="open a pull request\nthat scales it", observe_seconds=900.0)


def test_the_window_can_be_named():
    assert stop_hook.parse(f"{DIRECTIVE} observe=30\nask").observe_seconds == 30.0


@pytest.mark.parametrize(
    "first",
    [
        f"{DIRECTIVE} observe=0",
        f"{DIRECTIVE} observe=-5",
        f"{DIRECTIVE} observe=99999",
        f"{DIRECTIVE} observe=soon",
        f"{DIRECTIVE} later",
        f"{DIRECTIVE} observe=1 observe=2",
    ],
)
def test_a_bad_directive_is_refused(first):
    with pytest.raises(ValueError):
        stop_hook.parse(first + "\nask")


def test_a_directive_with_no_ask_is_refused():
    with pytest.raises(ValueError):
        stop_hook.parse(DIRECTIVE + "\n  ")


def test_the_api_transport_refuses_a_stopped_ask(monkeypatch):
    monkeypatch.setenv("AGENT_TRANSPORT", "api")
    got = harness.KubeAgentsHarness()._execute(f"{DIRECTIVE}\nask")
    assert DIRECTIVE in " ".join(got.errors)


def _cards(statuses):
    return board.SessionCards(session_found=True, card_ids=list(statuses), source="state.db",
                              cards={t: {"status": s} for t, s in statuses.items()})


MISSING = board.SessionCards(session_found=False, card_ids=[], source="state.db", cards={})

# The card ids the last run purged off the pod, and archived.
PURGED = []
ARCHIVED = []


@pytest.fixture(autouse=True)
def _no_pod(monkeypatch):
    """No test here reaches the real pod: the purge and the archive run
    `kubectl exec` against whatever kubeconfig the machine has. Sleeps
    advance a fake clock instead of waiting."""
    PURGED.clear()
    ARCHIVED.clear()
    monkeypatch.setattr(harness, "_purge_card_state", lambda ids, timeout: PURGED.extend(ids))
    monkeypatch.setattr(harness, "_archive_stalled_cards", lambda ids, timeout: ARCHIVED.extend(ids))
    clock = [0.0]
    monkeypatch.setattr(harness.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(harness.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))


def _stop(monkeypatch, reads, send=(True, "stopped"), observe=0.0, session="a2a-ctx-1"):
    """Run the stop with board reads answered from ``reads`` in order (the
    last repeats). Returns the result and how many times the stop was sent."""
    answers = list(reads)

    def read(*a, **k):
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(harness.board, "read_session_cards", read)
    sent = []

    def send_stop():
        sent.append(True)
        if isinstance(send, Exception):
            raise send
        return send

    result = AgentResult(output="on it", trajectory=[])
    harness.KubeAgentsHarness()._stop_after_first_card(
        result, session, stop_hook.Stop(prompt="ask", observe_seconds=observe),
        send_stop=send_stop, poll_interval=0.0,
    )
    return result, len(sent)


def _entry(result, name=stop_hook.STOP_ENTRY):
    return next(e for e in result.trajectory if e["name"] == name)


def test_a_filed_card_is_stopped_and_recorded(monkeypatch):
    result, sent = _stop(monkeypatch, [_cards({"t_1": "archived"})])
    assert sent == 1
    assert _entry(result)["status"] == "completed"
    assert _entry(result)["args"]["cards"] == ["t_1"]
    assert result.metadata["stop_card_statuses"] == {"t_1": "archived"}
    assert ARCHIVED == [] and PURGED == ["t_1"]
    assert _entry(result, stop_hook.CARDS_STOPPED_ENTRY)["status"] == "completed"


def test_the_stop_goes_before_any_read(monkeypatch):
    """The stop needs no card ids, so it goes first, and a card the turn
    saved a moment late is stopped too."""
    order = []
    monkeypatch.setattr(harness.board, "read_session_cards", lambda *a, **k: order.append("read") or _cards({"t_1": "archived"}))
    result = AgentResult(output="on it", trajectory=[])
    harness.KubeAgentsHarness()._stop_after_first_card(
        result, "a2a-ctx-1", stop_hook.Stop(prompt="ask", observe_seconds=0.0),
        send_stop=lambda: order.append("stop") or (True, "stopped"), poll_interval=0.0,
    )
    assert order[0] == "stop"


def test_the_verdict_is_the_windows_last_read_not_its_first(monkeypatch):
    """Fails if the verdict came from the read taken just after the stop:
    the archive lands later in the window."""
    result, _ = _stop(monkeypatch, [_cards({"t_1": "running"}), _cards({"t_1": "archived"})], observe=30.0)
    assert result.metadata["stop_card_statuses"] == {"t_1": "archived"}
    assert _entry(result, stop_hook.CARDS_STOPPED_ENTRY)["status"] == "completed"
    assert ARCHIVED == []


def test_a_card_archived_early_then_misread_late_is_graded_on_the_late_read(monkeypatch):
    # The converse: an early archived read does not excuse a late running one.
    result, _ = _stop(monkeypatch, [_cards({"t_1": "archived"}), _cards({"t_1": "running"})], observe=30.0)
    assert _entry(result, stop_hook.CARDS_STOPPED_ENTRY)["status"] == "error"
    assert ARCHIVED == ["t_1"]


def test_a_card_that_finished_instead_of_stopping_fails_the_stop(monkeypatch):
    # A card that reached done wrote whatever it wrote; the stop did not stop it.
    result, _ = _stop(monkeypatch, [_cards({"t_1": "done"})])
    assert _entry(result, stop_hook.CARDS_STOPPED_ENTRY)["status"] == "error"


def test_a_refused_stop_archives_and_purges_at_once_and_fails(monkeypatch):
    """A stop the gateway did not take is not watched: the cards are archived
    and purged straight away, and both entries fail."""
    result, _ = _stop(
        monkeypatch, [_cards({"t_1": "running"})], send=(False, "unverified author: who are you"), observe=900.0
    )
    assert harness.time.monotonic() < 900.0, "the window was waited out for a stop that did not go"
    assert ARCHIVED == ["t_1"] and PURGED == ["t_1"]
    assert _entry(result)["status"] == "error"
    assert _entry(result, stop_hook.CARDS_STOPPED_ENTRY)["status"] == "error"
    assert any("who are you" in e for e in result.errors)


def test_a_stop_the_gateway_started_a_task_for_fails_the_entry(monkeypatch):
    result, _ = _stop(monkeypatch, [_cards({"t_1": "running"})],
                      send=(False, "the gateway started task task-9 for the stop instead of stopping"))
    assert _entry(result)["status"] == "error"
    assert any("task-9" in e for e in result.errors)


def test_no_card_still_sends_the_stop_and_fails_the_entry(monkeypatch):
    result, sent = _stop(monkeypatch, [_cards({})])
    assert sent == 1
    assert _entry(result)["status"] == "error"
    assert any("filed no card" in e for e in result.errors)


def test_a_card_still_running_after_the_window_is_archived(monkeypatch):
    result, _ = _stop(monkeypatch, [_cards({"t_1": "running"})], observe=30.0)
    assert ARCHIVED == ["t_1"] and PURGED == ["t_1"]
    assert any("not terminal" in e for e in result.errors)
    assert _entry(result, stop_hook.CARDS_STOPPED_ENTRY)["status"] == "error"


def test_a_read_that_fails_once_is_retried(monkeypatch):
    result, _ = _stop(monkeypatch, [None, _cards({"t_1": "archived"})])
    assert _entry(result)["status"] == "completed"


def test_an_unreadable_session_still_sends_the_stop_and_is_infrastructure(monkeypatch):
    with pytest.raises(harness._TransportError, match="could not be read"):
        _stop(monkeypatch, [None])


def test_a_window_whose_last_read_fails_is_infrastructure_and_still_archives(monkeypatch):
    """The card an earlier read learned is archived and purged even though
    the last read failed: nothing says it ended."""
    with pytest.raises(harness._TransportError, match="could not be read"):
        _stop(monkeypatch, [_cards({"t_1": "running"})] + [None] * 10, observe=30.0)
    assert ARCHIVED == ["t_1"] and PURGED == ["t_1"]


def test_a_session_not_in_the_store_still_sends_the_stop_and_is_infrastructure(monkeypatch):
    with pytest.raises(harness._TransportError, match="not in the store"):
        _stop(monkeypatch, [MISSING])


def test_no_session_still_sends_the_stop_and_is_infrastructure(monkeypatch):
    monkeypatch.setattr(harness.board, "read_session_cards", lambda *a, **k: pytest.fail("read with no session"))
    sent = []
    result = AgentResult(output="on it", trajectory=[])
    with pytest.raises(harness._TransportError, match="no session to read"):
        harness.KubeAgentsHarness()._stop_after_first_card(
            result, "", stop_hook.Stop(prompt="ask", observe_seconds=0.0),
            send_stop=lambda: sent.append(True) or (True, "stopped"), poll_interval=0.0,
        )
    assert sent == [True]
    assert result.trajectory[-1]["status"] == "error"


def test_a_stop_the_door_never_carried_archives_purges_and_is_infrastructure(monkeypatch):
    unreachable = harness._TransportError("the stop could not reach the gateway: refused")
    with pytest.raises(harness._TransportError, match="could not reach the gateway"):
        _stop(monkeypatch, [_cards({"t_1": "running"})], send=unreachable, observe=900.0)
    assert ARCHIVED == ["t_1"] and PURGED == ["t_1"]
    assert harness.time.monotonic() < 900.0


@pytest.mark.parametrize("prompt", [f"\n{DIRECTIVE}\nask", f"{DIRECTIVE}observe=60\nask"])
def test_a_near_miss_directive_is_refused_not_sent_as_an_ask(prompt):
    if prompt.startswith("\n"):
        assert stop_hook.parse(prompt).prompt == "ask"
    else:
        with pytest.raises(ValueError, match="must stand alone"):
            stop_hook.parse(prompt)


def test_every_task_that_names_the_directive_is_a_stopped_ask_that_parses():
    import yaml

    tasks = harness.Path(__file__).resolve().parents[1] / "tasks"
    for path in sorted(tasks.glob("*/task.yaml")):
        prompt = str((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("prompt") or "")
        if DIRECTIVE in prompt:
            assert stop_hook.parse(prompt) is not None, path


def test_the_case_carries_the_directive_on_its_own_line():
    import yaml

    case = harness.Path(__file__).resolve().parents[1] / "tasks" / "chat-stop-halts-delegated-remediation" / "task.yaml"
    stop = stop_hook.parse(yaml.safe_load(case.read_text(encoding="utf-8"))["prompt"])
    assert stop is not None and "checkout-gateway" in stop.prompt
