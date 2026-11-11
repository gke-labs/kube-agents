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

# The shape of the leak: a count drawn from a finished card, with the card
# named. Every fixture here is made up; no run's report text is committed
# (.agents/rules/public_surfaces.md).
LEAK = (
    "*platform reported 12 running GKE clusters across the 4 GCP projects in scope on card "
    "t_0badcafe.* Want me to have `platform` run a fresh count?"
)
# Two finished counts on the board, each named with its id and a time.
RED_RUN = (
    "Per the `platform` specialist's counts on the board from earlier today (task t_0badcafe at 14:20 UTC, "
    "about 2 hours ago, and task t_deadbeef at 03:05 UTC), there are 12 running GKE clusters (with 36 total "
    "nodes) across all 4 GCP projects in scope. Want me to have `platform` run a fresh live count now?"
)
# The same answer with the id in another dress: bare, after a colon, in a link.
LEAKS = (
    LEAK,
    RED_RUN,
    "platform counted 12 clusters earlier today (task t_0badcafe).",
    "**12 GKE clusters**, from platform's count at 14:02 UTC: t_0badcafe.",
    "platform found 12 GKE clusters about an hour ago; see [the card](https://example.invalid/t_0badcafe).",
)
# What the rule asks for: the count and when as a first sentence, who
# counted, any breakdown after, and no id.
GOOD = (
    "platform counted 12 running GKE clusters across your 4 projects about 3 hours ago. One cluster is in "
    "acme-prod, 3 are in acme-staging, and 4 each are in the 2 acme-dev projects, with 36 total nodes.",
    "platform counted 12 running GKE clusters across all 4 GCP projects in scope about 3 hours ago. That "
    "includes 1 cluster in acme-prod, 3 in acme-staging, and 4 each across the 2 acme-dev projects.",
    "platform counted 12 running GKE clusters (with 36 total nodes) across all 4 GCP projects in scope "
    "about two hours ago: 1 in acme-prod, 3 in acme-staging, and 4 in each of the 2 acme-dev projects.",
    "**platform counted 12 running GKE clusters across your 4 projects about an hour ago.** Want a fresh count?",
    "**12 GKE clusters are running across the 4 projects in scope**, per platform's count two hours old.",
    "**12 GKE clusters are running**, from platform's count as of 14:02.",
    "**platform's count at 14:02 UTC found 12 running GKE clusters.**",
    "platform counted 12 running GKE clusters at about 14:02 UTC.",
    "platform counted 12 running GKE clusters around 14:02 UTC.",
    "platform counted 12 running GKE clusters (14:02 UTC).",
    "**12 GKE clusters**, counted by platform on 2026-11-11.",
)
# A fresh card: the ack, then the result the harness folds under its header.
FRESH = (
    "checking all 4 clusters.\n\nResult of delegated task t_0badcafe:\n"
    "**12 GKE clusters are running across 4 projects.** 11 are RUNNING and 1 is RECONCILING."
)
# A fresh card whose agent-side words still carry the id.
FRESH_LEAK = (
    "checking all clusters (card t_0badcafe).\n\nResult of delegated task t_0badcafe:\n"
    "**12 GKE clusters are running across 4 projects.**"
)
# A fresh card that came back with no count: the ack's roster number is not
# a count, and a clock time's tail is not one either.
FRESH_NO_COUNT = (
    "checking all 3 clusters.\n\nResult of delegated task t_0badcafe:\nThe count could not be taken.",
    "i don't have a count on the board as of 14:20 utc for your clusters.",
)
# No count at all: an offer, or a refusal; the project count from the roster
# block is not a cluster count.
NO_COUNT = (
    "I don't have a count from today. Want me to have platform run one?",
    "I can't see the fleet myself.",
    "No count from earlier today. Want platform to count the 42 projects' clusters?",
)
# A time of day with no staleness in it does not say when.
VAGUE_WHEN = (
    "platform counted 209 clusters earlier today.",
    "209 clusters, per platform, today.",
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
    for text in NO_COUNT + FRESH_NO_COUNT:
        assert not check_passes(CASE, CHECK_COUNT, text), text


def test_a_bare_today_does_not_say_when():
    for text in VAGUE_WHEN:
        assert not check_passes(CASE, CHECK_WHEN, text), text


def test_the_id_shape_survives_the_surface():
    # The surface drops the underscore as Markdown emphasis; the pattern
    # makes it optional, so the id is caught in either spelling.
    assert pattern_surface("card t_0badcafe") == "card t0badcafe"
    assert not check_passes(CASE, CHECK_NO_ID, "card t0badcafe holds the count of 209 clusters")


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
