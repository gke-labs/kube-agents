"""Tests for ``chat_shape``: the reply's shape, measured the same way on every run.

The replies are transcribed from Slack screenshots of real Kage threads (the
UX refresh deck's "ugly" set and its mocks), trimmed where noted; the lead-in
openers are the shapes the refresh set out to remove.
"""

from __future__ import annotations

import pytest
from kube_agents_bench import chat_shape

ACK = "checking payments-api in seeded-a."


def _delegated(*results: str, ack: str = ACK) -> str:
    sections = [f"Result of delegated task t_{i:08x}:\n{r}" for i, r in enumerate(results)]
    return "\n\n".join([ack, *sections])


# Kubeagents 3, 2026-10-14: a bold lead, then a report under `##` headings with
# fenced YAML and three numbered options. Trimmed after the second option.
CRASHLOOP_REPORT = """\
**The payments-api workload is CrashLooping due to an OOMKill (exit code 137) caused by the container running tail /dev/zero, which rapidly exhausts its memory.**
The `payments-api` workload in the `seeded-debug` namespace is failing to start and is stuck in `CrashLoopBackOff` due to being `OOMKilled` (exit code 137).

## Root Cause
The container is executing a command that consumes infinite memory unbounded until it hits its limit. Specifically, the deployment runs:
```yaml
containers:
- command:
  - sh
  - -c
  - tail /dev/zero
```
Running `tail` on `/dev/zero` continually reads null bytes into memory. The container is configured with a strict memory limit of `64Mi`, which is exhausted almost immediately upon startup, leading the node to kill the process.

## Evidence
- The pod is in `CrashLoopBackOff` state.
- The deployment manifest defines the memory limit at `64Mi`.

## Remediation Options
To fix the restarts, we need to address the root cause of the memory exhaustion.
1. **Option 1 (Recommended): Fix the command**
   Change the command in the deployment manifest to `tail -f /dev/null` or `sleep infinity`.
2. **Option 2: Increase the memory limit**
   If reading from `/dev/zero` is a legitimate requirement for this workload, the memory limit of `64Mi` needs to be significantly increased.

Which of these options would you like to proceed with?"""

# kagenext, the compliance audit's daily run: narration of what ran, then
# bold labels. Names no verdict, but nothing structural marks it.
AUDIT_NARRATION = """\
An absolute read-only, fleet-wide compliance-audit of GKE clusters in the `bnaylor-kagents-fleet` project has been executed and reported using the declarative GitOps workflow.

The ledger issue on the GitHub repository has been successfully updated with the findings, and since there were no new or resolved findings compared to the previous run, this daily scheduled watchdog run is silent.

**Audit Stream:** `compliance-audit`
**Ledger URL:** https://github.com/sackheads/kage-bug-bash/issues/4
**Audit Status:** [SILENT]"""

# kube-agents, a fix delivered as a PR: the verdict first, then sections.
PR_VERDICT = """\
Both fixes are up for review in PR #99 — but `checkout-api` was never in git, so the PR also has to bring it under Argo CD, and the Secret it now reads has to be created by hand. No cluster was mutated.

What the PR changes
- `DATABASE_URL` on container `api`, as a `secretKeyRef` to key `dsn` of Secret `orders-db-credentials` in `storefront`. No credential is committed."""

# The refresh's mock for the same moment: one bold sentence and one line of evidence.
PR_MOCK = (
    "**I opened PR #412 to raise checkout-gateway's memory limit to 512Mi. It's yours to review.**\n"
    "OOMKilled 14× in 1h · limit 256Mi, p99 410Mi"
)


def test_a_bold_verdict_opens_the_answer_even_under_a_wall_of_report():
    shape = chat_shape.measure(_delegated(CRASHLOOP_REPORT))
    assert shape[chat_shape.OPENS_ON_VERDICT] is True
    assert shape[chat_shape.WORDS_BEFORE_ANSWER] == 0
    assert shape[chat_shape.BOT_MESSAGES] == 2
    # The wall is what answer_words is for; the refresh's mock answer is 24.
    assert shape[chat_shape.ANSWER_WORDS] > 200
    assert chat_shape.measure(PR_MOCK)[chat_shape.ANSWER_WORDS] == 24


def test_narration_that_names_no_verdict_is_left_to_the_judge():
    shape = chat_shape.measure(AUDIT_NARRATION)
    assert shape[chat_shape.OPENS_ON_VERDICT] is True
    assert shape[chat_shape.BOT_MESSAGES] == 1


def test_a_plain_verdict_line_followed_by_a_section_label_opens_on_it():
    shape = chat_shape.measure(PR_VERDICT)
    assert shape[chat_shape.OPENS_ON_VERDICT] is True
    assert shape[chat_shape.WORDS_BEFORE_ANSWER] == 0
    assert shape[chat_shape.APOLOGY] is False


@pytest.mark.parametrize(
    ("reply", "before"),
    [
        ("Let me check the pods in seeded-a.\n**Two pods are OOMKilled.**", 7),
        ("Sure! **Two pods are OOMKilled.**", 1),
        ("I'll look into this. Two pods are OOMKilled.", 4),
        ("Here's what I found:\n\n**Two pods are OOMKilled.**", 4),
        ("## Summary\nTwo pods are OOMKilled.", 2),
        ("**Pod status:**\nTwo pods are OOMKilled.", 2),
        ("✅ Okay, checking now.\nTwo pods are OOMKilled.", 4),
        ("Sure. Let me check.\n## Findings\nTwo pods are OOMKilled.", 6),
        ("**Sure!** Two pods are OOMKilled.", 1),
        ('"Got it." Two pods are OOMKilled.', 2),
        ("I\u2019ll look into this. Two pods are OOMKilled.", 4),
        ("Let\u2019s see. Two pods are OOMKilled.", 2),
        ("\u2018Got it.\u2019 Two pods are OOMKilled.", 2),
        ("Sure (checking now). Two pods are OOMKilled.", 3),
    ],
)
def test_a_lead_in_before_the_answer_is_counted(reply: str, before: int):
    shape = chat_shape.measure(reply)
    assert shape[chat_shape.OPENS_ON_VERDICT] is False
    assert shape[chat_shape.WORDS_BEFORE_ANSWER] == before


def test_a_lead_in_word_later_in_the_answer_is_not_a_preamble():
    shape = chat_shape.measure("**Two pods are OOMKilled.** I'll raise the limit if you say so.")
    assert shape[chat_shape.OPENS_ON_VERDICT] is True
    assert shape[chat_shape.WORDS_BEFORE_ANSWER] == 0


def test_a_word_that_only_starts_like_a_lead_in_is_not_one():
    shape = chat_shape.measure("Surely-failing probes: none. OKD is not installed.")
    assert shape[chat_shape.OPENS_ON_VERDICT] is True


def test_a_fenced_comment_is_no_heading():
    shape = chat_shape.measure("```\n# kubectl get pods\n```\nTwo pods are OOMKilled.")
    assert shape[chat_shape.OPENS_ON_VERDICT] is True


def test_an_answer_that_is_all_preamble_stops_counting():
    shape = chat_shape.measure("Let me check.\nLet me look.\nLet me see.\nLet me try.")
    assert shape[chat_shape.OPENS_ON_VERDICT] is False
    assert shape[chat_shape.WORDS_BEFORE_ANSWER] == 9


@pytest.mark.parametrize(
    "reply",
    [
        "Sorry, I couldn't reach seeded-b.",
        "I apologize for the delay: two pods are OOMKilled.",
        "My apologies, the earlier answer was wrong.",
    ],
)
def test_an_apology_is_flagged(reply: str):
    assert chat_shape.measure(reply)[chat_shape.APOLOGY] is True


@pytest.mark.parametrize(
    "reply",
    [
        "**payments-api is failing readiness.**\n```\nHTTP 503: sorry, upstream unavailable\n```",
        "**payments-api is failing readiness:** the probe gets `503 sorry, upstream unavailable`.",
    ],
)
def test_an_apology_inside_quoted_code_is_not_the_replys(reply: str):
    assert chat_shape.measure(reply)[chat_shape.APOLOGY] is False


def test_an_apology_in_the_ack_counts_too():
    final = _delegated(PR_MOCK, ack="Sorry for the wait, checking now.")
    assert chat_shape.measure(final)[chat_shape.APOLOGY] is True


def test_the_answer_is_the_last_delivered_result():
    final = _delegated("Let me check seeded-a.", PR_MOCK)
    assert chat_shape.answer_text(final) == PR_MOCK
    shape = chat_shape.measure(final)
    assert shape[chat_shape.OPENS_ON_VERDICT] is True
    assert shape[chat_shape.BOT_MESSAGES] == 3


def test_questions_and_artifacts_are_messages_but_not_the_answer():
    final = "\n\n".join(
        [
            ACK,
            "Question from delegated task t_1:\nWhich option should I take?",
            "Result of delegated task t_1:\n" + PR_MOCK,
            "Artifact plan.md produced by delegated task t_1:\n# Plan",
        ]
    )
    assert chat_shape.answer_text(final) == PR_MOCK
    # The artifact is a file read back from the pod, not a post.
    assert chat_shape.measure(final)[chat_shape.BOT_MESSAGES] == 3


def test_an_apology_quoted_in_an_artifact_is_not_the_replys():
    final = _delegated(PR_MOCK) + "\n\nArtifact report.md produced by delegated task t_1:\nsorry, connection refused"
    shape = chat_shape.measure(final)
    assert shape[chat_shape.APOLOGY] is False
    assert shape[chat_shape.BOT_MESSAGES] == 2


def test_a_card_that_paused_on_a_question_is_answered_by_the_question():
    final = "\n\n".join([ACK, "Question from delegated task t_1:\nWhich option should I take?"])
    assert chat_shape.answer_text(final) == "Which option should I take?"
    shape = chat_shape.measure(final)
    assert shape[chat_shape.OPENS_ON_VERDICT] is True
    assert shape[chat_shape.ANSWER_WORDS] == 5
    assert shape[chat_shape.BOT_MESSAGES] == 2


def test_an_artifact_alone_leaves_the_reply_as_the_answer():
    final = "\n\n".join(["**Two pods are OOMKilled.**", "Artifact report.md produced by delegated task t_1:\n# Report"])
    assert chat_shape.answer_text(final) == "**Two pods are OOMKilled.**"


def test_a_reply_with_no_ack_counts_only_its_sections():
    final = "Result of delegated task t_1:\n" + PR_MOCK
    assert chat_shape.measure(final)[chat_shape.BOT_MESSAGES] == 1


def test_the_transports_post_count_wins():
    assert chat_shape.measure(PR_MOCK, posts=3)[chat_shape.BOT_MESSAGES] == 3


@pytest.mark.parametrize("final", ["", "  \n", _delegated("")])
def test_nothing_delivered_is_not_measured(final: str):
    assert chat_shape.measure(final) is None


def test_parse_keeps_only_well_typed_measurements():
    data = {
        chat_shape.OPENS_ON_VERDICT: True,
        chat_shape.APOLOGY: 0,
        chat_shape.BOT_MESSAGES: 2,
        chat_shape.WORDS_BEFORE_ANSWER: -1,
        chat_shape.ANSWER_WORDS: True,
        "extra": 1,
    }
    assert chat_shape.parse(data) == {chat_shape.OPENS_ON_VERDICT: True, chat_shape.BOT_MESSAGES: 2}


@pytest.mark.parametrize("data", [None, [], "x", {}, {"extra": 1}])
def test_parse_of_nothing_valid_is_none(data):
    assert chat_shape.parse(data) is None


def test_a_measurement_round_trips_through_parse():
    shape = chat_shape.measure(_delegated(CRASHLOOP_REPORT))
    assert chat_shape.parse(shape) == shape
