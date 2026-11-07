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


def test_a_quoted_artifact_is_neither_appended_nor_marked():
    base = _base("The answer already quotes: the report body.")
    _append_final(base, ["Artifact report.md produced by delegated task t1:\nthe report body."])
    assert base.metadata["final_message"] == "The answer already quotes: the report body."


def test_append_final_with_no_sections_is_a_no_op():
    base = _base("unchanged")
    _append_final(base, [])
    assert base.metadata["final_message"] == "unchanged"


# -------------------------------------- a card result relayed verbatim

# The kanban smoke's specialist check: the card's own result, read through the
# header the harness writes, names the planted fleet.
_KANBAN_SMOKE_CASE = "agent-kanban-smoke"
_SPECIALIST_CHECK = "the-specialists-answer-names-the-planted-fleet"
_RELAYED_RESULT = (
    "**Five GKE clusters are running in project `evals-5`.**\n\n"
    "| Cluster | Status |\n| --- | --- |\n"
    "| `seeded-a` | `RUNNING` |\n| `seeded-b` | `RUNNING` |\n| `seeded-c` | `RUNNING` |"
)
_SECTION_T1 = f"Result of delegated task t1:\n{_RELAYED_RESULT}"


def _kanban_smoke_specialist_patterns() -> list[str]:
    from pathlib import Path

    import yaml

    doc = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "tasks" / _KANBAN_SMOKE_CASE / "task.yaml").read_text()
    )
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == _SPECIALIST_CHECK]
    return check["check"]["required_patterns"]


def test_a_card_result_the_router_relayed_verbatim_keeps_its_header():
    base = _base(_RELAYED_RESULT)
    _append_delivered(base, _observed_with_card_result("t1", _RELAYED_RESULT), ["t1"])
    assert (base.output, base.metadata["final_message"]) == (_SECTION_T1, _SECTION_T1)


def test_a_relay_inside_the_routers_prose_is_lifted_out_behind_the_prose():
    base = _base(f"Here is what the specialist found: {_RELAYED_RESULT}\n\nAnything else?")
    _append_delivered(base, _observed_with_card_result("t1", _RELAYED_RESULT), ["t1"])
    assert base.metadata["final_message"] == f"Here is what the specialist found:\n\nAnything else?\n\n{_SECTION_T1}"


def test_the_last_occurrence_of_a_short_result_is_the_relay():
    body = "All pods are Running."
    base = _base(f"I asked card t1 whether {body} The specialist said: {body}")
    _append_delivered(base, _observed_with_card_result("t1", body), ["t1"])
    assert base.metadata["final_message"] == (
        f"I asked card t1 whether {body} The specialist said:\n\nResult of delegated task t1:\n{body}"
    )


def test_folding_the_same_result_twice_adds_nothing():
    base = _base(_RELAYED_RESULT)
    observed = _observed_with_card_result("t1", _RELAYED_RESULT)
    _append_delivered(base, observed, ["t1"])
    once = (base.output, base.metadata["final_message"])
    _append_delivered(base, observed, ["t1"])
    assert (base.output, base.metadata["final_message"]) == once


def test_a_body_already_under_its_header_with_a_blank_line_is_left_alone():
    already = f"Result of delegated task t1:\n\n{_RELAYED_RESULT}"
    base = _base(already)
    _append_final(base, [_SECTION_T1])
    assert base.metadata["final_message"] == already


def test_two_cards_with_the_same_result_each_keep_a_full_section():
    base = _base("Delegated to t1 and t2.")
    _append_final(base, [_SECTION_T1, f"Result of delegated task t2:\n{_RELAYED_RESULT}"])
    final = base.metadata["final_message"]
    assert final == f"Delegated to t1 and t2.\n\n{_SECTION_T1}\n\nResult of delegated task t2:\n{_RELAYED_RESULT}"


def test_a_later_card_whose_answer_repeats_part_of_an_earlier_ones_cuts_nothing():
    first = f"**Clusters in evals-5:**\n{_RELAYED_RESULT}"
    base = _base("Delegated to t1 and t2.")
    _append_final(base, [f"Result of delegated task t1:\n{first}", f"Result of delegated task t2:\n{_RELAYED_RESULT}"])
    assert base.metadata["final_message"] == (
        f"Delegated to t1 and t2.\n\nResult of delegated task t1:\n{first}\n\nResult of delegated task t2:\n{_RELAYED_RESULT}"
    )


def test_a_relay_is_lifted_only_out_of_the_prose_ahead_of_the_sections():
    base = _base(f"{_RELAYED_RESULT}\n\nArtifact a.md produced by delegated task t1:\n{_RELAYED_RESULT}")
    _append_final(base, [_SECTION_T1])
    assert base.metadata["final_message"] == (
        f"Artifact a.md produced by delegated task t1:\n{_RELAYED_RESULT}\n\n{_SECTION_T1}"
    )


def test_an_artifact_keeps_its_indented_first_line():
    section = "Artifact a.yaml produced by delegated task t1:\n  key: v\n  k2: v2"
    base = _base("Read the file.")
    _append_final(base, [section])
    assert base.metadata["final_message"] == f"Read the file.\n\n{section}"


def test_the_kanban_smokes_specialist_check_reads_a_verbatim_relay():
    base = _base(_RELAYED_RESULT)
    _append_delivered(base, _observed_with_card_result("t1", _RELAYED_RESULT), ["t1"])
    transcript.set(base.output, base.trajectory, final_message=base.metadata["final_message"])
    check = ReportContainsVerifier(type="report_contains", required_patterns=_kanban_smoke_specialist_patterns())
    assert check.verify(5.0).status == "pass"


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


_NEGATORS = (
    "no|not|never|cannot|can'?t|don'?t|didn'?t|does not|doesn'?t|is not|isn'?t"
    "|was not|wasn'?t|were not|weren'?t|aren'?t|won'?t|wouldn'?t|shouldn'?t"
    "|couldn'?t|without|nothing|none|neither|nor|unable to|rather than"
    "|non(?=-?guarant)"
)
_SEGMENT_CHAR = "(?:[^.!?\\n]|[.!?](?!\\s|$))"
# A segment with no negation before "guarant" fails, and a negation's
# influence is bounded: 60 non-negation characters after it, the ban
# re-arms. The task files must carry this exact pattern; a test below pins
# them to it.
GUARANTEE_PATTERN = (
    f"(?:^|\\n|[.!?](?=\\s|$))(?:(?!\\b(?:{_NEGATORS})\\b){_SEGMENT_CHAR})*guarant"
    f"|\\b(?:{_NEGATORS})\\b(?:(?!\\b(?:{_NEGATORS})\\b){_SEGMENT_CHAR}){{60,}}guarant"
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


def test_every_negator_is_load_bearing_one_sentence_each():
    # One sentence per negator: dropping any word from the alternation
    # makes its sentence flag and this test fail, which is what pins the
    # list (a single multi-negator sentence tests only its first word).
    for sentence in (
        "Spot capacity is not guaranteed.",
        "There is no guarantee of allocation.",
        "We don't guarantee the window.",
        "It cannot guarantee placement.",
        "The API doesn't guarantee a start.",
        "Reservations are without guarantee.",
        "Nothing here guarantees capacity.",
        "None of the paths guarantees a start.",
        "Neither path is guaranteed.",
        "A non-guaranteed pool may be reclaimed.",
        "It wasn't guaranteed last week.",
        "Flex-Start won't guarantee the slot.",
        "Placement couldn't be guaranteed.",
        "We are unable to guarantee a start.",
        "It holds capacity rather than guarantees it.",
    ):
        _set_final(sentence)
        check = ReportContainsVerifier(
            type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
        )
        assert check.verify(5.0).status == "pass", sentence


def test_a_typographic_apostrophe_negation_is_recognized():
    _set_final("We don’t guarantee capacity in either zone.")
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "pass"


def test_a_non_compound_does_not_mask_a_later_banned_word():
    _set_final("A non-preemptible reservation guarantees capacity.")
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "fail"


def test_a_negations_influence_expires_after_sixty_characters():
    _set_final(
        "No risk in Spot today, and the region has ample headroom across "
        "all zones and families, so the reservation guarantees capacity."
    )
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "fail"


def test_a_forbidden_pattern_scopes_negation_to_its_own_sentence():
    _set_final("No guarantee exists for Spot. Flex-Start guarantees a window.")
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "fail"


def test_the_task_files_carry_this_exact_pattern():
    from pathlib import Path

    import yaml

    tasks = Path(__file__).resolve().parent.parent / "tasks"
    for case in (
        "obtainability-design-quota-vs-capacity",
        "obtainability-window-planning-probe",
    ):
        doc = yaml.safe_load((tasks / case / "task.yaml").read_text())
        patterns = [
            check["check"]["forbidden_patterns"]
            for check in doc["verification_spec"]
            if isinstance(check.get("check"), dict)
            and check["check"].get("forbidden_patterns")
        ]
        assert patterns == [[GUARANTEE_PATTERN]], case


def test_a_negated_bullet_does_not_mask_the_next_bullets_banned_word():
    _set_final(
        "- Spot: **not** a reserved path\n- Flex-Start guarantees the window"
    )
    check = ReportContainsVerifier(
        type="report_contains", forbidden_patterns=[GUARANTEE_PATTERN]
    )
    assert check.verify(5.0).status == "fail"


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
