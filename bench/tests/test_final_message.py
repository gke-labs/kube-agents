# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The run-level final_message composition.

final_message is what the user ultimately receives: the delegating turn's own
closing message plus, when work was delegated, the delivered card results and
artifacts. Poll-turn recitals are excluded. Both failure shapes this pins were
real: a later settle overwriting the kanban answer ("created, the id is 7" ->
"the card settled"), and a delegated worker's RCA never reaching the default
verifier scope because only result.output carried it.
"""

from __future__ import annotations

import json

import pytest

from devops_bench.agents import AgentResult

from kube_agents_bench import transcript
from kube_agents_bench.harness import _append_delivered, _append_final, _fold_status_turn
from kube_agents_bench.verifiers import ReportContainsVerifier


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def _base(answer: str) -> AgentResult:
    res = AgentResult(output=answer, trajectory=[])
    res.metadata["final_message"] = answer
    return res


def _poll_turn(closer: str) -> AgentResult:
    turn = AgentResult(output=closer, trajectory=[])
    turn.metadata["final_message"] = closer
    return turn


# ------------------------------------------------- kanban shape (1a)


def test_a_settled_recital_does_not_replace_the_delegating_answer():
    base = _base("Created the kanban task; the id is 7.")
    _fold_status_turn(base, _poll_turn("Task 7 settled successfully."), settled=True)
    assert base.metadata["final_message"] == "Created the kanban task; the id is 7."
    # the recital still reaches the judge's accumulated text
    assert "settled successfully" in base.output


def test_two_settles_still_keep_the_original_answer():
    base = _base("Created the kanban task; the id is 7.")
    _fold_status_turn(base, _poll_turn("still working"), settled=False)
    _fold_status_turn(base, _poll_turn("card 7 done"), settled=True)
    _fold_status_turn(base, _poll_turn("all cards done"), settled=True)
    assert base.metadata["final_message"] == "Created the kanban task; the id is 7."


# --------------------------------------------- delegated shape (1b)


def _observed_with_card_result(tid: str, text: str) -> list[dict]:
    return [
        {
            "name": "kanban_show",
            "args": {},
            "result": json.dumps({"task": {"id": tid, "result": text}}),
            "status": "completed",
        }
    ]


def test_a_delivered_card_result_reaches_final_message():
    base = _base("Delegated the investigation to card t1.")
    rca = "Root cause: GCS FUSE buffer exhaustion during checkpoint load."
    _append_delivered(base, _observed_with_card_result("t1", rca), ["t1"])
    assert rca in base.metadata["final_message"]
    assert base.metadata["final_message"].startswith("Delegated the investigation")
    assert rca in base.output


def test_the_verifier_default_scope_sees_the_workers_rca_not_the_recital():
    base = _base("Delegated the investigation to card t1.")
    _fold_status_turn(
        base, _poll_turn("card t1 mentions gcsfuse trouble, still running"), settled=False
    )
    _append_delivered(
        base,
        _observed_with_card_result("t1", "Root cause: GCS FUSE buffer exhaustion."),
        ["t1"],
    )
    transcript.set(
        base.output,
        base.trajectory,
        final_message=str(base.metadata.get("final_message") or ""),
    )
    check = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["gcs fuse", "gcsfuse"]
    )
    assert check.verify(5.0).status == "pass"


def test_a_phrase_only_in_a_poll_recital_does_not_satisfy_final_scope():
    base = _base("Delegated the investigation to card t1.")
    _fold_status_turn(
        base, _poll_turn("progress: worker suspects GCS FUSE exhaustion"), settled=True
    )
    transcript.set(
        base.output,
        base.trajectory,
        final_message=str(base.metadata.get("final_message") or ""),
    )
    final = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["gcs fuse", "gcsfuse"]
    )
    full = ReportContainsVerifier(
        type="report_contains", any_of_phrases=["gcs fuse", "gcsfuse"], scope="full"
    )
    assert final.verify(5.0).status == "fail"
    assert full.verify(5.0).status == "pass"


# ------------------------------------------------------ _append_final


def test_append_final_deduplicates_against_the_existing_message():
    base = _base("The answer already quotes: the report body.")
    _append_final(base, ["Artifact report.md produced by delegated task t1:\nthe report body."])
    assert base.metadata["final_message"].count("the report body") == 1


def test_append_final_with_no_sections_is_a_no_op():
    base = _base("unchanged")
    _append_final(base, [])
    assert base.metadata["final_message"] == "unchanged"


# ------------------------------------------------- the ledger pointer


def test_a_delegated_audits_ledger_url_reaches_the_default_scope():
    """The plumbing ``ledger_issue_contains`` stands on.

    A fleet audit runs in a delegated worker, whose tool calls reach
    result.trajectory only as clipped, tagged entries no verifier reads for
    content, and its SOP keeps the router's own closing line to one sentence
    with no findings in it. What DOES cross back is the worker's card result,
    which the SOP requires to carry issue_url in full — and that is the only
    channel by which the verifier can learn WHICH issue to read.
    """
    base = _base("Compliance audit complete; details in the ledger.")
    url = "https://github.com/gke-agentic/kube-agents-evals-infra/issues/42"
    _append_delivered(
        base,
        _observed_with_card_result("t1", f"UPDATED — 1 new finding. Ledger: {url}"),
        ["t1"],
    )
    assert url in base.metadata["final_message"]


# --------------------------------------- report_contains forbidden_patterns


GUARANTEE_PATTERN = (
    "(?:^|[.!?](?=\\s|$)|\\n)(?:(?!\\b(?:no|not|never|cannot|can't|does not"
    "|doesn't|is not|isn't|aren't|won't|without|non|nothing|none|neither)\\b)"
    "(?:[^.!?\\n]|[.!?](?!\\s|$)))*guarant"
)


def _set_final(answer: str) -> None:
    base = _base(answer)
    transcript.set(
        base.output,
        base.trajectory,
        final_message=str(base.metadata.get("final_message") or ""),
    )


def test_a_forbidden_pattern_flags_the_unnegated_banned_word():
    _set_final("Spot may be reclaimed. The reservation guarantees capacity.")
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    result = check.verify(5.0)
    assert result.status == "fail"
    assert "forbidden patterns matched" in result.reason


def test_a_forbidden_pattern_permits_the_negated_uses():
    _set_final(
        "Capacity is not guaranteed; there is no guarantee of allocation, "
        "and a non-guaranteed pool may be reclaimed without notice."
    )
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    result = check.verify(5.0)
    assert result.status == "pass"
    assert "forbidden pattern(s)" in result.reason


def test_a_forbidden_pattern_scopes_negation_to_its_own_sentence():
    _set_final("No guarantee exists for Spot. Flex-Start guarantees a window.")
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "fail"


def test_a_negated_bullet_does_not_mask_the_next_bullets_banned_word():
    _set_final(
        "- Spot: **not** a reserved path\n- Flex-Start guarantees the window"
    )
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "fail"


def test_common_negations_beyond_test01s_list_are_recognized():
    _set_final("Nothing here guarantees capacity; neither path is guaranteed.")
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "pass"


def test_a_decimal_does_not_split_a_negation_from_the_word_it_negates():
    _set_final("Flex-Start is not a 99.9% guarantee of a start.")
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "pass"


def test_a_real_sentence_end_after_a_decimal_still_arms_the_ban():
    _set_final("Obtainability scored 0.9. Flex-Start guarantees the window.")
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "fail"


def test_an_uncompilable_forbidden_pattern_is_rejected_at_construction():
    with pytest.raises(Exception):
        ReportContainsVerifier(type="report_contains", forbidden_patterns=["(unclosed"])
