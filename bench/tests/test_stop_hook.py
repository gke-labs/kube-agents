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


def _run(monkeypatch, reads, send=(True, "stopped")):
    """Run the stop with board reads answered from ``reads`` in order."""
    calls = iter(reads)
    monkeypatch.setattr(harness.board, "read_session_cards", lambda *a, **k: next(calls))
    archived = []
    monkeypatch.setattr(harness, "_archive_stalled_cards", lambda ids, timeout: archived.extend(ids))
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    sent = []
    result = AgentResult(output="on it", trajectory=[])
    harness.KubeAgentsHarness()._stop_after_first_card(
        result,
        "a2a-ctx-1",
        stop_hook.Stop(prompt="ask", observe_seconds=0.0),
        send_stop=lambda: sent.append(True) or send,
        poll_interval=0.0,
    )
    entry = next(e for e in result.trajectory if e["name"] == stop_hook.STOP_ENTRY)
    return result, entry, sent, archived


def test_a_filed_card_is_stopped_and_recorded(monkeypatch):
    result, entry, sent, archived = _run(monkeypatch, [_cards({"t_1": "running"}), _cards({"t_1": "archived"})])
    assert sent == [True]
    assert entry["status"] == "completed"
    assert entry["args"]["cards"] == ["t_1"]
    assert result.metadata["stop_card_statuses"] == {"t_1": "archived"}
    assert archived == []


def test_no_card_sends_no_stop_and_fails_the_entry(monkeypatch):
    result, entry, sent, _ = _run(monkeypatch, [_cards({})])
    assert sent == []
    assert entry["status"] == "error"
    assert result.errors


def test_a_stop_the_gateway_did_not_take_fails_the_entry(monkeypatch):
    result, entry, _, _ = _run(monkeypatch, [_cards({"t_1": "running"}), _cards({"t_1": "running"})],
                               send=(False, "the gateway started task task-9 for the stop instead of stopping"))
    assert entry["status"] == "error"
    assert any("task-9" in e for e in result.errors)


def test_a_card_still_running_after_the_window_is_archived(monkeypatch):
    result, _, _, archived = _run(monkeypatch, [_cards({"t_1": "running"}), _cards({"t_1": "running"})])
    assert archived == ["t_1"]
    assert any("not terminal" in e for e in result.errors)


def test_an_unreadable_session_sends_no_stop(monkeypatch):
    _, entry, sent, _ = _run(monkeypatch, [None])
    assert sent == [] and entry["status"] == "error"


def test_no_session_sends_no_stop(monkeypatch):
    monkeypatch.setattr(harness.board, "read_session_cards", lambda *a, **k: pytest.fail("read with no session"))
    result = AgentResult(output="on it", trajectory=[])
    harness.KubeAgentsHarness()._stop_after_first_card(
        result, "", stop_hook.Stop(prompt="ask", observe_seconds=0.0),
        send_stop=lambda: pytest.fail("sent with no session"), poll_interval=0.0,
    )
    assert result.trajectory[-1]["status"] == "error"


def test_the_case_carries_the_directive_on_its_own_line():
    import yaml

    case = harness.Path(__file__).resolve().parents[1] / "tasks" / "chat-stop-halts-delegated-remediation" / "task.yaml"
    stop = stop_hook.parse(yaml.safe_load(case.read_text(encoding="utf-8"))["prompt"])
    assert stop is not None and "checkout-gateway" in stop.prompt


