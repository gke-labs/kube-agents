"""chat-voice-reused-answer-no-card-id grades the agent's words, not the harness's headers.

The case forbids a card id (hermes' ``t_`` + 8 hex) on any line of the final
message except the delivered-section headers the harness writes itself
(``harness._RESULT_SECTION``, ``_QUESTION_SECTION`` and the artifact header),
requires the count, and requires a reused count to say when it was taken
(or the delivered header, which is what a fresh card arrives under). These
fixtures run each check as the verifier grades it (``conftest.check_passes``)
on the leaked answer, on the intended one, and on a fresh card's fold, so a
change to the harness's header or to ``pattern_surface`` reds this test
rather than the nightly. The last test pins the header literals the
lookahead excludes to the harness's own.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from conftest import check_passes

from kube_agents_bench import harness
from kube_agents_bench.verifiers import pattern_surface

CASE = "chat-voice-reused-answer-no-card-id"
CHECK_NO_ID = "the-answer-carries-no-card-id"
CHECK_COUNT = "the-answer-gives-the-count"
CHECK_WHEN = "a-reused-count-says-when-it-was-taken"
CHECKS = (CHECK_NO_ID, CHECK_COUNT, CHECK_WHEN)

# The answer the gkedemos front door gave on 2026-11-11, as it reached the user.
LEAK = (
    "*platform reported 209 running GKE clusters across the 42 GCP projects in scope on card "
    "t_7bd8e9a5.* Want me to have `platform` run a fresh count?"
)
# The red run on main bd74a6e25 (2026-11-11, gkedemos): two ids, each with a time.
RED_RUN = (
    "Per the `platform` specialist's counts on the board from earlier today (task t_78c29f59 at 14:20 UTC, "
    "about 2 hours ago, and task t_7bd8e9a5 at 03:05 UTC), there are 209 running GKE clusters (with 563 total "
    "nodes) across all 42 GCP projects in scope. Want me to have `platform` run a fresh live count now?"
)
# The same answer with the id in another dress: bare, after a colon, in a link.
LEAKS = (
    LEAK,
    RED_RUN,
    "platform counted 209 clusters earlier today (task t_7bd8e9a5).",
    "**209 GKE clusters**, from platform's count at 14:02 UTC: t_7bd8e9a5.",
    "platform found 209 GKE clusters about an hour ago; see [the card](https://example.invalid/t_7bd8e9a5).",
)
# What the rule asks for: the count, who counted, and when, with no id. The
# first three are the green runs on the branch's image (2026-11-11, gkedemos).
GOOD = (
    "platform counted 209 running GKE clusters (with 563 total nodes) across all 42 GCP projects in scope "
    "about 2 hours ago: 1 cluster in bhoekstra-gkedemos, 8 in devops-bench-complex-evals, and 5 in each of "
    "the 40 kube-agents-evals projects.",
    "platform counted 209 running GKE clusters (with 563 total nodes) across all 42 GCP projects in scope "
    "about two hours ago: 1 in bhoekstra-gkedemos, 8 in devops-bench-complex-evals, and 5 in each of the 40 "
    "kube-agents-evals projects. Want me to run a fresh count?",
    "platform counted 209 running GKE clusters (with 563 total nodes) across all 42 GCP projects in scope "
    "about two hours ago. That includes 1 cluster in bhoekstra-gkedemos, 8 in devops-bench-complex-evals, "
    "and 5 in each of the 40 kube-agents-evals projects.",
    "**platform counted 209 running GKE clusters across your 42 projects about an hour ago.** Want a fresh count?",
    "**209 GKE clusters are running across the 42 projects in scope**, per platform's count earlier today.",
    "**platform's count at 14:02 UTC found 209 running GKE clusters.**",
    "**209 GKE clusters**, counted by platform on 2026-11-11.",
)
# A fresh card: the ack, then the result the harness folds under its header.
FRESH = (
    "checking all clusters.\n\nResult of delegated task t_7bd8e9a5:\n"
    "**209 GKE clusters are running across 42 projects.** 203 are RUNNING and 6 are RECONCILING."
)
# A fresh card whose agent-side words still carry the id.
FRESH_LEAK = (
    "checking all clusters (card t_7bd8e9a5).\n\nResult of delegated task t_7bd8e9a5:\n"
    "**209 GKE clusters are running across 42 projects.**"
)
# No count at all: an offer, or a refusal.
NO_COUNT = (
    "I don't have a count from today. Want me to have platform run one?",
    "I can't see the fleet myself.",
)


def test_the_leaked_answer_fails_the_id_check_and_the_when_check():
    for text in LEAKS:
        assert not check_passes(CASE, CHECK_NO_ID, text), text
    assert check_passes(CASE, CHECK_COUNT, LEAK)
    assert not check_passes(CASE, CHECK_WHEN, LEAK)


def test_the_intended_answers_pass_every_check():
    for text in GOOD:
        for check in CHECKS:
            assert check_passes(CASE, check, text), (check, text)


def test_a_fresh_cards_fold_passes_on_the_harness_header_alone():
    for check in CHECKS:
        assert check_passes(CASE, check, FRESH), check
    assert not check_passes(CASE, CHECK_NO_ID, FRESH_LEAK)


def test_an_answer_without_a_count_fails_the_count_check():
    for text in NO_COUNT:
        assert not check_passes(CASE, CHECK_COUNT, text), text


def test_the_id_shape_survives_the_surface():
    # The surface drops the underscore as Markdown emphasis; the pattern
    # makes it optional, so the id is caught in either spelling.
    assert pattern_surface("card t_7bd8e9a5") == "card t7bd8e9a5"
    assert not check_passes(CASE, CHECK_NO_ID, "card t7bd8e9a5 holds the count of 209 clusters")


def test_the_excluded_headers_are_the_harness_own():
    task = Path(__file__).resolve().parents[1] / "tasks" / CASE / "task.yaml"
    (check,) = [c for c in yaml.safe_load(task.read_text())["verification_spec"] if c["name"] == CHECK_NO_ID]
    (pattern,) = check["check"]["forbidden_patterns"]
    for header in harness._RELAYED_SECTION_PREFIXES:
        assert pattern_surface(header).strip() in pattern, header
    assert "produced by delegated task" in pattern
    # The when check's header alternative is the result header, surfaced.
    (when,) = [c for c in yaml.safe_load(task.read_text())["verification_spec"] if c["name"] == CHECK_WHEN]
    assert when["check"]["any_of_patterns"][0] == "(?m)^" + pattern_surface(harness._RELAYED_SECTION_PREFIXES[0]).strip() + r"\b"
