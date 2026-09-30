"""Reusable portal interaction execution and evidence projection helpers."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cuj.utils.evidence import EvidenceLog
from cuj.utils.portal import Portal, PortalError, portal_token

if TYPE_CHECKING:
    from cuj.utils.scenario import ScenarioConfig

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}

#: The hand-off Kage is told to send (agents/chat/SOUL.md §2, step 4) is one
#: line naming what it is checking:
#:
#:     checking checkout-gateway.
#:
#: The template fixes the lowercase and the period but not the verb, the
#: length of the target, or whether it is a link, and a model drifts on all of
#: them, so ``_is_progress_ack`` matches the shape rather than the words: one
#: line opening with an ``-ing`` verb in either case, then a target of up to
#: ``_ACK_MAX_TARGET_WORDS`` words, with or without a closing period or
#: ellipsis. What keeps an answer from matching is what an answer carries and
#: a target does not:
#:
#: - before any clause opener, a verdict ("looking very good."), a finding
#:   verb ("checking the events shows the pod was evicted.") or a past tense
#:   that no determiner turns into an adjective ("restarting the pod cleared
#:   it.", while "reviewing the failed rollout." is a hand-off); after a
#:   clause opener all three are part of the target ("checking why the
#:   rollout failed.");
#: - a label and a value, bold or not: "Running pods: 12.";
#: - a clause after a comma, opening with a subject or a number or carrying a
#:   past tense: "looking at the events, nothing stands out." (a
#:   comma-separated list of targets is still a hand-off);
#: - an opening that is a noun or adjective, not a verb: "nothing is
#:   restarting.", "Missing quota in us-central1."
#:
#: ``_DELEGATION_ACK`` is the receipt that template replaced, still sent by an
#: install on an older image:
#:
#:     > 🔀 Delegated to the **<agent-name>** agent
#:
#:     I've started this as task `<task_id>`. The answer will post into this
#:     thread as soon as it's ready.
#:
#: Matching has to survive that formatting — the agent name arrives wrapped in
#: bold markers and the task id in backticks — and must not fire on a report
#: that merely cites its own task id. Each of those branches therefore pairs
#: hand-off phrasing with the thing handed off.
#:
#: Each receipt branch is the template's own wording, not a paraphrase of it: "Results
#: for the design will post below" and "Assigned under task t_...: start at
#: 14:00" are answers that a looser matcher stripped.
_DELEGATION_ACK = re.compile(
    r"\bdelegat(?:ed|ing)\b[^.\n]{0,60}\b\**\w[\w-]*\**\s+agent\b"
    r"|\bstarted this as task\s+[`'\"]?t_[0-9a-f]+"
    r"|\bwill post into this thread\b",
    re.IGNORECASE,
)

#: The most words a hand-off's target runs to after its verb. "scaling
#: checkout-gateway down to two replicas in prod-a." is seven.
_ACK_MAX_TARGET_WORDS = 12
_ACK_VERB = re.compile(r"[^\W\d_]{2,}ing", re.IGNORECASE)
#: Words ending in "ing" that open an answer rather than a hand-off.
_ACK_NOT_VERBS = frozenset(
    {
        "nothing", "everything", "something", "anything", "during", "missing",
        "pending", "existing", "remaining", "following", "outstanding",
        "interesting", "matching",
    }
)
_ACK_VERDICTS = frozenset(
    {
        "good", "fine", "great", "healthy", "ok", "okay", "well", "bad", "better",
        "worse", "normal", "clean",
    }
)
_ACK_FINDING_VERBS = frozenset(
    {
        "is", "isn't", "was", "wasn't", "are", "aren't", "were", "weren't", "shows",
        "showed", "shown", "found", "finds", "reveals", "revealed", "confirms",
        "confirmed", "indicates", "indicated", "returned", "returns", "says",
        "said", "looks", "seems", "appears",
    }
)
_ACK_CLAUSE_OPENERS = frozenset(
    {
        "why", "whether", "if", "what", "how", "where", "which", "when", "who",
        "that",
    }
)
#: Words that make a following "-ed" word an adjective in the target.
_ACK_DETERMINERS = frozenset(
    {
        "the", "a", "an", "this", "that", "these", "those", "my", "your", "its",
        "their", "our", "each", "every", "all", "any", "some", "no",
    }
)
#: Words that open a clause after a comma, where a list would name a target.
_ACK_CLAUSE_SUBJECTS = frozenset(
    {
        "the", "it", "there", "nothing", "everything", "something", "none", "i",
        "we", "they", "this", "all", "no",
    }
)
_ACK_CLOSING = re.compile(r"(?:\.{1,3}|…)\Z")
_ACK_WORD_WRAPPING = "*_`[]()\"'"
_ACK_PAST_TENSE = "ed"


@dataclass(frozen=True)
class InteractionRunner:
    config: ScenarioConfig
    log: EvidenceLog
    approval_choice: str = "deny"

    def run(self, prompt: str, *, session_prefix: str) -> dict[str, Any]:
        request = {
            "agentId": self.config.agent_id,
            "profile": self.config.profile,
            "sessionId": f"{session_prefix}_{uuid.uuid4().hex}",
            "input": {"text": prompt},
            "history": [],
        }
        self.log.record("request", request)

        portal = Portal(self.config.endpoint, token=portal_token())
        interaction = portal.post("interactions", request)
        self.log.record("interaction", {"poll": 0, "value": interaction})
        # Polling repeats the whole projection every couple of seconds, and
        # an unchanged repeat says nothing a reader needs: a 15-minute run
        # wrote 275 KB of near-identical payloads and buried the four moments
        # that mattered. Only transitions are recorded from here, plus the
        # terminal state, which the summary and every evaluator read.
        previous = json.dumps(interaction, sort_keys=True, default=str)
        interaction_id = str(interaction.get("interactionId") or "")
        if not interaction_id:
            raise PortalError("portal response did not include interactionId")

        deadline = time.monotonic() + self.config.timeout
        poll = 0
        while str(interaction.get("status") or "") not in TERMINAL_STATUSES:
            if time.monotonic() >= deadline:
                interaction = {**interaction, "evaluatorTimedOut": True}
                self.log.record(
                    "interaction",
                    {"poll": poll, "value": interaction},
                )
                break
            if interaction.get("status") == "waiting_for_approval":
                interaction = portal.post(
                    "interactions/"
                    f"{urllib.parse.quote(interaction_id, safe='')}/approval",
                    {"choice": self.approval_choice},
                )
            else:
                time.sleep(self.config.poll_interval)
                interaction = portal.get(
                    f"interactions/{urllib.parse.quote(interaction_id, safe='')}"
                )
            poll += 1
            current = json.dumps(interaction, sort_keys=True, default=str)
            if current != previous:
                self.log.record(
                    "interaction",
                    {"poll": poll, "value": interaction},
                )
                previous = current

        # The loop above recorded the terminal projection when it appeared, so
        # this marker carries the poll count and status only.
        self.log.record(
            "interaction_final",
            {"poll": poll, "status": interaction.get("status")},
        )
        return interaction


def projected_tasks(
    interaction: dict[str, Any], *, assignee: str = ""
) -> list[dict[str, Any]]:
    tasks = [
        task for task in interaction.get("tasks", []) if isinstance(task, dict)
    ]
    if assignee:
        return [task for task in tasks if task.get("assignee") == assignee]
    return tasks


def projected_records(
    interaction: dict[str, Any],
    field: str,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for source in (interaction, *projected_tasks(interaction)):
        values = source.get(field, [])
        if isinstance(values, list):
            found.extend(item for item in values if isinstance(item, dict))
    return found


def completed_evidence(interaction: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for task in projected_tasks(interaction):
        for item in task.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").casefold()
            if status in {"completed", "passed"}:
                found.add(str(item.get("type") or ""))
    return found


def projected_tool_calls(interaction: dict[str, Any]) -> list[dict[str, Any]]:
    calls = interaction.get("toolCalls", [])
    found = (
        [call for call in calls if isinstance(call, dict)]
        if isinstance(calls, list)
        else []
    )
    for task in projected_tasks(interaction):
        task_calls = task.get("toolCalls", [])
        if isinstance(task_calls, list):
            found.extend(call for call in task_calls if isinstance(call, dict))
    return found


def tool_operations(
    interaction: dict[str, Any], *, completed_only: bool = False
) -> list[str]:
    return [
        str(call.get("operation") or call.get("name") or "")
        for call in projected_tool_calls(interaction)
        if not completed_only or call.get("status") == "completed"
    ]


def _is_progress_ack(sentence: str) -> bool:
    """Whether ``sentence`` is the one-line hand-off, per the comment on
    ``_DELEGATION_ACK``."""

    text = sentence.strip()
    if "\n" in text:
        return False
    body = _ACK_CLOSING.sub("", text)
    words = body.split()
    if not 2 <= len(words) <= _ACK_MAX_TARGET_WORDS + 1:
        return False
    bare = [word.strip(_ACK_WORD_WRAPPING).rstrip(",").casefold() for word in words]
    if not _ACK_VERB.fullmatch(bare[0]) or bare[0] in _ACK_NOT_VERBS:
        return False
    if any(";" in word or word.endswith(":") for word in bare):
        return False
    for previous, word in zip(bare, bare[1:]):
        if word in _ACK_CLAUSE_OPENERS:
            break
        if word in _ACK_VERDICTS or word in _ACK_FINDING_VERBS:
            return False
        if word.endswith(_ACK_PAST_TENSE) and previous not in _ACK_DETERMINERS:
            return False
    if "," in body:
        after = [
            word.strip(_ACK_WORD_WRAPPING).casefold()
            for word in body.rsplit(",", 1)[1].split()
        ]
        if after and after[0] in _ACK_CLAUSE_SUBJECTS:
            return False
        if any(
            word.endswith(_ACK_PAST_TENSE) or word[:1].isdigit() for word in after
        ):
            return False
    return True


def substantive_output(interaction: dict[str, Any]) -> str:
    """The user-visible answer with leading delegation acknowledgments removed.

    Acknowledgments are dropped sentence by sentence rather than paragraph by
    paragraph: a coordinator that opens its answer with "Delegated to the
    platform agent. Here is the design: ..." must keep the design, while an
    interaction that only ever acknowledged returns the empty string — that
    silence is the finding, not something to paper over.
    """

    text = str(interaction.get("output") or "")
    kept: list[str] = []
    skipping = True
    for paragraph in re.split(r"\n\s*\n", text):
        if not skipping:
            kept.append(paragraph)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip())
        remainder = [sentence for sentence in sentences if sentence.strip()]
        while remainder and (
            _DELEGATION_ACK.search(remainder[0]) or _is_progress_ack(remainder[0])
        ):
            remainder.pop(0)
        if remainder:
            skipping = False
            kept.append(" ".join(remainder))
    return "\n\n".join(kept).strip()


def delivered_answer(interaction: dict[str, Any]) -> str:
    """Everything the user reads for this interaction, acknowledgments removed.

    The coordinator's reply is the hand-off alone by instruction (SOUL.md,
    Planning Loop step 4); the specialist's ``result`` is posted into the same
    thread by the gateway without passing back through the coordinator. A
    criterion scored on "the answer the user received" therefore reads both:
    the substantive part of the root output, then each projected task's
    result, in task order. Where the projection carries no task results the
    value is the root output alone, which is what earlier criteria scored.
    """

    parts = [substantive_output(interaction)]
    for task in projected_tasks(interaction):
        result = task.get("result")
        if isinstance(result, str) and result.strip():
            parts.append(result.strip())
    return "\n\n".join(part for part in parts if part)


def latest_artifact(
    artifacts: list[dict[str, Any]],
    *,
    kind: str = "",
    artifact_type: str = "",
) -> dict[str, Any] | None:
    """The most recent artifact matching a manifest kind and/or record type.

    Latest wins: a worker that attaches a corrected manifest supersedes its
    earlier attempt, exactly as a re-uploaded file would. Both CUJ scenarios
    read artifacts through this, so they cannot grade the same recorder
    behavior in opposite directions.
    """

    for artifact in reversed(artifacts):
        manifest = artifact.get("manifest")
        if not isinstance(manifest, dict):
            continue
        if kind and manifest.get("kind") != kind:
            continue
        if artifact_type and artifact.get("type") != artifact_type:
            continue
        return artifact
    return None


def unnormalized_tool_calls(interaction: dict[str, Any]) -> list[str]:
    """Tool calls whose mutation impact cannot be judged from the projection."""

    return [
        str(call.get("name") or "<unnamed>")
        for call in projected_tool_calls(interaction)
        if not str(call.get("operation") or "").strip()
    ]
