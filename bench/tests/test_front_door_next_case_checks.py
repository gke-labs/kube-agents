"""The outcome checks the front-door cases grade on spec.mode next (#4004).

Each case's checks are read out of its task file and run against replies the
harness would fold into ``final_message`` on the inject lane, so a pattern edit
that stops accepting a good reply, or starts accepting a bad one, fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASKS = Path(__file__).resolve().parents[1] / "tasks"

ACK = "checking payments-api."
QUESTION = "Question from delegated task t_1a2b3c4d:\n"
RESULT = "Result of delegated task t_1a2b3c4d:\n"


def _report_checks(case: str) -> list[dict]:
    spec = yaml.safe_load((TASKS / case / "task.yaml").read_text(encoding="utf-8"))
    return [e["check"] for e in spec["verification_spec"] if e["check"]["type"] == "report_contains"]


def _passes(case: str, delivered: str) -> bool:
    transcript.set(ACK, [], final_message=f"{ACK}\n\n{delivered}")
    return all(ReportContainsVerifier(**c).verify(timeout_sec=1.0).success for c in _report_checks(case))


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


PAUSES = "chat-question-asked-to-choose-pauses"


@pytest.mark.parametrize(
    ("delivered", "passes"),
    [
        pytest.param(
            QUESTION + "**payments-api is OOMKilled** against its 64Mi limit on every start.\n\n"
            "Which one?\n- Raise the limit to 256Mi\n- Set a request to match\n- Reduce the app's memory use",
            True,
            id="dash options after a bold finding",
        ),
        pytest.param(
            QUESTION + "payments-api is OOMKilled (exit code 137).\nWhich fix should I apply?\n* Raise the limit\n* Set a request",
            False,
            id="star bullets, which normalization leaves looking like wrapped lines",
        ),
        pytest.param(
            QUESTION + "payments-api keeps dying: OOMKilled.\n\nWhich one?\n\n1. Raise the limit\n\n2. Lower usage\n",
            True,
            id="numbered options with blank lines between",
        ),
        pytest.param(QUESTION + "Which fix for payments-api (OOMKilled)?\n- a\n- b", True, id="the question opens the reason"),
        pytest.param(
            QUESTION + "payments-api OOMKilled.\nWhich one?\n- a\n- b\n\n" + RESULT.replace("t_1a2b3c4d", "t_9") + "other",
            True,
            id="another card's result follows",
        ),
        pytest.param(
            RESULT + "payments-api is OOMKilled.\nWhich one?\n- Raise\n- Set",
            False,
            id="the card completed with the options as its result",
        ),
        pytest.param(QUESTION + "payments-api OOMKilled.\nWhich one?\n- Raise the limit", False, id="one option"),
        pytest.param(
            QUESTION + "payments-api OOMKilled.\nWhich one?\n- a\n- b\n- c\n- d\n- e\n- f", False, id="six options"
        ),
        pytest.param(
            QUESTION + "payments-api OOMKilled.\nWhich one?\n- a\n- b\nLet me know.", False, id="text after the options"
        ),
        pytest.param(
            QUESTION + "payments-api uses too much memory.\nWhich one?\n- Increase the memory limit\n- b",
            False,
            id="no OOM evidence beside the options' own memory",
        ),
        pytest.param(
            QUESTION + "payments-api OOMKilled.\nWhich one?\n- " + "x" * 80 + "\n- b",
            False,
            id="an option too long for a button",
        ),
        pytest.param(
            QUESTION + "payments-api is OOMKilled against its 64Mi limit. Shall I go on?\n\n"
            + QUESTION.replace("t_1a2b3c4d", "t_2") + "Which one?\n- Raise the limit\n- Set a request",
            False,
            id="the OOM in one card's question and the options in another's",
        ),
        pytest.param(
            QUESTION + "payments-api OOMKilled.\nWhich one?\n- Raise the limit to\n  256Mi\n- Set a request",
            False,
            id="a wrapped option line, which the Slack parser does not read as an option",
        ),
        pytest.param(
            QUESTION + "payments-api OOMKilled.\nWhich one?\n  I see two choices\n- a\n- b",
            False,
            id="an indented line before the options",
        ),
        pytest.param(
            QUESTION + "payments-api OOMKilled.\nWhich one?\n- a\n- b\nArtifact logs are attached.",
            False,
            id="a line after the options that only starts like an artifact header",
        ),
    ],
)
def test_the_pause_is_graded_on_the_question_the_conversation_shows(delivered: str, passes: bool) -> None:
    assert _passes(PAUSES, delivered) is passes


ROUTING = "chat-routing-fleet-question"


@pytest.mark.parametrize(
    ("delivered", "passes"),
    [
        pytest.param(
            RESULT + "Four clusters: seeded-a, seeded-b and seeded-c on 1.33.5-gke.1080000, "
            "platform-agent-host on 1.34.1-gke.100.",
            True,
            id="the fleet and its versions",
        ),
        pytest.param(RESULT + "seeded-a, seeded-b and seeded-c, all on GKE v1.33.5.", True, id="a v-prefixed version"),
        pytest.param(RESULT + "seeded-a, seeded-b and seeded-c are running.", False, id="no version"),
        pytest.param(RESULT + "Three clusters on 1.33: seeded-a and seeded-b.", False, id="a seeded cluster missing"),
        pytest.param(
            RESULT + "seeded-a, seeded-b and seeded-c, at 10.1.20.3 and 10.1.30.4.", False, id="an IP is not a version"
        ),
        pytest.param(RESULT + "seeded-a, seeded-b and seeded-c, at 1.20.3.4.", False, id="nor one that opens with 1"),
        pytest.param(
            RESULT + "seeded-a, seeded-b and seeded-c, each with 1.25Gi allocatable.", False, id="a quantity in Gi"
        ),
        pytest.param(
            RESULT + "seeded-a, seeded-b and seeded-c use 1.50 vCPU and cost $1.20 an hour.",
            False,
            id="a vCPU count and a price",
        ),
        pytest.param(RESULT + "seeded-a, seeded-b and seeded-c run 1.33.", False, id="a bare minor version"),
        pytest.param(
            RESULT + "seeded-a, seeded-b and seeded-c: p50 1.20 ms, load 1.25, 1.50 GHz.",
            False,
            id="a latency, a load average and a clock speed",
        ),
        pytest.param(
            "seeded-a, seeded-b and seeded-c, all on 1.33.5.", False, id="the front door answered with no card result"
        ),
    ],
)
def test_the_routed_fleet_question_is_graded_on_the_specialists_answer(delivered: str, passes: bool) -> None:
    assert _passes(ROUTING, delivered) is passes


SMOKE = "agent-kanban-smoke"


@pytest.mark.parametrize(
    ("delivered", "passes"),
    [
        pytest.param(RESULT + "seeded-a\nseeded-b\nseeded-c\nplatform-agent-host", True, id="the specialist's list"),
        pytest.param("seeded-a, seeded-b and seeded-c.", False, id="the front door answered with no card result"),
        pytest.param(
            "seeded-a, seeded-b and seeded-c.\n\n" + RESULT + "seeded-a only.",
            False,
            id="the names outside the card result",
        ),
    ],
)
def test_the_smoke_is_graded_on_the_specialists_answer(delivered: str, passes: bool) -> None:
    assert _passes(SMOKE, delivered) is passes


ACK_CASE = "chat-voice-ack-names-target"


def _ack_passes(final_message: str) -> bool:
    transcript.set(final_message, [], final_message=final_message)
    return all(ReportContainsVerifier(**c).verify(timeout_sec=1.0).success for c in _report_checks(ACK_CASE))


def test_the_ack_needs_the_specialists_answer_after_it() -> None:
    answer = RESULT + "checkout-gateway is healthy: 0 restarts in 24h."
    assert _ack_passes("checking checkout-gateway.\n\n" + answer)
    assert not _ack_passes("checking checkout-gateway.")


def test_a_question_section_ends_the_front_doors_own_text() -> None:
    """A delegated question that says "let me know" is the specialist's
    voice, not the front door's."""
    answer = RESULT + "checkout-gateway is healthy."
    question = QUESTION.replace("t_1a2b3c4d", "t_2") + "Which cluster should I check? Let me know."
    assert _ack_passes(f"checking checkout-gateway.\n\n{answer}\n\n{question}")


def test_a_paused_specialist_also_follows_the_ack() -> None:
    question = QUESTION + "checkout-gateway runs on two clusters. Which should I check?\n- seeded-a\n- seeded-b"
    assert _ack_passes("checking checkout-gateway.\n\n" + question)


NEXT_STEP = "chat-answer-declares-next-step"


@pytest.mark.parametrize(
    ("delivered", "passes"),
    [
        pytest.param(
            RESULT + "payments-api is OOMKilled against its 64Mi limit.\n[next: raise the memory limit]",
            True,
            id="one step at the end",
        ),
        pytest.param(
            RESULT + "payments-api is OOMKilled against its 64Mi limit.\n[next: raise the memory limit]\n\n"
            + QUESTION.replace("t_1a2b3c4d", "t_2")
            + "Which cluster?\n- seeded-a\n- seeded-b",
            True,
            id="one step, then another card's question",
        ),
        pytest.param(
            RESULT + "payments-api is OOMKilled.\n[next: raise the limit]\n[next: lower usage]",
            False,
            id="two steps",
        ),
        pytest.param(RESULT + "payments-api is OOMKilled. Want me to raise the limit?", False, id="an offer in prose"),
    ],
)
def test_the_next_step_ends_the_answer_before_any_question_section(delivered: str, passes: bool) -> None:
    transcript.set(ACK, [], final_message=f"{ACK}\n\n{delivered}")
    checks = _report_checks(NEXT_STEP)
    assert all(ReportContainsVerifier(**c).verify(timeout_sec=1.0).success for c in checks) is passes
