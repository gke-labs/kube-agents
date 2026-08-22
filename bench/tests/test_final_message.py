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
