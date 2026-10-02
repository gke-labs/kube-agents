"""Tests for ``answer_first``: a delivered card result opens on its answer and stops."""

from __future__ import annotations

import pytest
from devops_bench.verification.spec import parse_node
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import AnswerFirstVerifier
from pydantic import ValidationError

ACK = "checking platform-agent-host."

# The mock's answer: one bold sentence, the evidence, an offer.
GOOD = (
    "**Good news: no pod outside kube-system is failing and no node is under memory "
    "pressure.** All three nodes report `MemoryPressure=False`, and the only pods not "
    "Running are two completed jobs. Want me to keep an eye on it?"
)

# The reply a slot-3 build delivered for the same ask, trimmed: a bold lead, a
# recap of it, then a report with headings.
REPORT = (
    "**All pods outside `kube-system` are Running, and no node is under MemoryPressure.**\n\n"
    "The cluster is healthy across the requested checks.\n\n"
    "## Pod Status\n"
    "I queried for pods in all namespaces where the phase is not `Running`.\n\n"
    "## Sources\n"
    "- `kubectl get pods --all-namespaces`\n"
)


def _verifier(**fields) -> AnswerFirstVerifier:
    return AnswerFirstVerifier(type="answer_first", **fields)


def _run(*results: str, **fields):
    sections = [f"Result of delegated task t_{i:08x}:\n{r}" for i, r in enumerate(results)]
    transcript.set(ACK, [], final_message="\n\n".join([ACK, *sections]))
    return _verifier(**fields).verify(timeout_sec=1.0)


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def test_the_mock_answer_passes():
    outcome = _run(GOOD, lead_terms=["pod", "memory"], recap_patterns=[r"cluster is healthy"])
    assert outcome.success, outcome.reason


def test_the_delivered_report_fails_on_each_defect():
    outcome = _run(REPORT, recap_patterns=[r"cluster is healthy"])
    assert not outcome.success
    assert "section heading" in outcome.reason
    assert "restates its verdict" in outcome.reason


def test_a_plain_lead_fails():
    outcome = _run(GOOD.replace("**", ""))
    assert not outcome.success
    assert "does not open with a bold sentence" in outcome.reason


def test_bold_punctuation_outside_the_span_still_reads_as_one_lead():
    outcome = _run("**It is not restarting**. Both pods have been up for 45h.")
    assert outcome.success, outcome.reason


def test_a_two_sentence_bold_lead_fails():
    outcome = _run("**It is not restarting. Both pods are up.** Uptime is 45h.")
    assert "more than one sentence" in outcome.reason


def test_lead_terms_are_matched_like_report_phrases():
    # "memory" finds MemoryPressure once emphasis and case are folded.
    assert _run("**No node has `MemoryPressure`.** All three are fine.", lead_terms=["memory"]).success
    outcome = _run("**All good.** No node has memory pressure.", lead_terms=["memory"])
    assert "no bold lead mentions ['memory']" in outcome.reason


def test_lead_terms_may_be_answered_across_two_cards():
    outcome = _run(
        "**No pod outside kube-system is failing.** All run.",
        "**No node is under memory pressure.** All three report False.",
        lead_terms=["pod", "memory"],
    )
    assert outcome.success, outcome.reason


def test_a_bold_fragment_is_not_a_lead():
    for result in (
        "**No** node is under memory pressure. All three report False.",
        "**No node is under memory pressure** and all pods run.",
    ):
        assert "not a whole sentence" in _run(result).reason, result


def test_a_lead_on_its_own_line_needs_no_full_stop():
    assert _run("**No node is under memory pressure**\n\nAll three report False.").success


def test_abbreviations_end_no_sentence():
    outcome = _run("**Every node reports pressure False, e.g. node-a.** Checked at 10 a.m. vs. yesterday.")
    assert outcome.success, outcome.reason


def test_the_caps_count_characters_and_sentences():
    long = "**It is fine.** " + "Evidence sentence. " * 40
    assert "characters, over 600" in _run(long).reason
    bullets = "**It is fine.**\n- one\n- two\n- three\n- four"
    assert "5 sentences, over 4" in _run(bullets).reason


def test_a_recap_in_the_lead_itself_is_not_a_restatement():
    outcome = _run("**The cluster is healthy.** All pods run.", recap_patterns=[r"cluster is healthy"])
    assert outcome.success, outcome.reason


def test_every_delivered_result_must_pass():
    outcome = _run(GOOD, REPORT)
    assert not outcome.success
    assert outcome.reason.startswith("result 2:")


def test_an_artifact_is_not_read_as_a_result():
    message = "\n\n".join(
        [ACK, f"Result of delegated task t_1:\n{GOOD}", f"Artifact r.md produced by delegated task t_1:\n{REPORT}"]
    )
    transcript.set(ACK, [], final_message=message)
    assert _verifier().verify(timeout_sec=1.0).success


def test_no_delivered_result_fails():
    transcript.set(ACK, [])
    outcome = _verifier().verify(timeout_sec=1.0)
    assert not outcome.success
    assert outcome.status != "error"


def test_an_empty_stash_errors():
    assert _verifier().verify(timeout_sec=1.0).status == "error"


def test_a_bad_recap_pattern_is_rejected_at_load():
    with pytest.raises(ValidationError):
        _verifier(recap_patterns=["("])


def test_it_resolves_through_the_spec_parser():
    node = parse_node({"type": "answer_first", "lead_terms": ["pod"]})
    assert node is not None
