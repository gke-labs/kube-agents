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
#: line opening with a hand-off verb from ``_ACK_VERBS`` in either case, then
#: a target of up to ``_ACK_MAX_TARGET_WORDS`` words, with or without a
#: closing period or ellipsis. A line opening with any other word is an
#: answer ("staging cluster has 3 nodes.", "running normally."). Past the
#: verb, what keeps an answer from matching is what an answer carries and a
#: target does not:
#:
#: - before any clause opener, a verdict ("looking very good."), a finite
#:   verb from ``_ACK_FINDING_VERBS`` ("restarting the pod took 3 minutes.",
#:   "restarting requires approval.") or a past tense ("restarting the pod
#:   cleared it."); after a clause opener all three are part of the target
#:   ("checking why the rollout failed.");
#: - an object after a verb: a determiner or pronoun from ``_ACK_OBJECTS``
#:   that does not follow the hand-off verb, a preposition, a particle or a
#:   conjunction ("restarting fixed it.", "draining evicts the pods."), where
#:   a target's own determiner follows one of those ("looking at the logs.");
#: - an ``-ed`` word is read as an adjective in the target, not a past tense,
#:   after a determiner or preposition ("reviewing the failed rollout.",
#:   "looking for orphaned disks.") or straight after the verb when a noun
#:   follows it ("reviewing failed rollouts in prod-a.", while "provisioning
#:   failed again." and "upgrading failed silently." are answers). A word
#:   ending "-eed" or holding a hyphen or digit is a noun or a name, not a
#:   past tense ("checking the fleet's seed.", "checking node-pool-red.");
#: - a label and a value, bold or not: "Running pods: 12.";
#: - a clause after the last comma that opens with a subject, holds a number
#:   or a past tense, or runs to ``_ACK_MIN_CLAUSE_WORDS`` words without a
#:   list joiner: "looking at the events, nothing stands out.", "checking the
#:   rollout, replicas never became ready." (a comma-separated list of
#:   targets, "checking seeded-a, seeded-b and seeded-c.", is a hand-off);
#: - a closing question mark or exclamation mark: "draining node-3 in
#:   prod-a — confirm?" asks the user something, and a hand-off does not.
#:
#: It is a heuristic over free text: an answer that opens with a hand-off
#: verb and carries none of these (a present-tense verb outside the lists,
#: followed by a bare noun, say) is still read as a hand-off.
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
#: Verbs a hand-off opens with. A closed list rather than any "-ing" word:
#: "staging", "warning" and "running" open answers as often as hand-offs.
_ACK_VERBS = frozenset(
    {
        "checking", "looking", "reviewing", "auditing", "investigating",
        "inspecting", "examining", "verifying", "diagnosing", "tracing",
        "querying", "searching", "comparing", "analyzing", "analysing",
        "provisioning", "scaling", "creating", "deploying", "upgrading",
        "updating", "patching", "applying", "installing", "removing",
        "deleting", "migrating", "resizing", "restarting", "draining",
        "cordoning", "rolling", "drafting", "planning", "designing", "pulling",
        "fetching", "gathering", "reading", "testing", "validating",
        "confirming", "evaluating", "measuring", "profiling", "estimating",
        "sizing", "digging", "triaging",
    }
)
_ACK_VERDICTS = frozenset(
    {
        "good", "fine", "great", "healthy", "ok", "okay", "well", "bad", "better",
        "worse", "normal", "clean",
    }
)
#: Finite verbs and auxiliaries an answer's clause carries and a target does
#: not.
_ACK_FINDING_VERBS = frozenset(
    {
        "is", "isn't", "was", "wasn't", "are", "aren't", "were", "weren't", "shows",
        "showed", "shown", "found", "finds", "reveals", "revealed", "confirms",
        "confirmed", "indicates", "indicated", "returned", "returns", "says",
        "said", "looks", "seems", "appears", "has", "hasn't", "have", "haven't",
        "had", "will", "won't", "can", "can't", "cannot", "could", "couldn't",
        "should", "would", "did", "didn't", "does", "doesn't", "gave", "gives",
        "got", "gets", "took", "takes", "brought", "brings", "went", "came",
        "made", "helps", "became", "becomes", "needs", "requires", "costs",
        "causes", "fixes", "breaks", "fails", "works", "succeeds", "remains",
        "stays", "means", "lacks", "uses", "restores", "evicts", "kills",
        "crashes", "exceeds", "hits",
    }
)
_ACK_CLAUSE_OPENERS = frozenset(
    {
        "why", "whether", "if", "what", "how", "where", "which", "when", "who",
        "that",
    }
)
#: Words that make a following "-ed" word an adjective in the target.
_ACK_ADJECTIVE_CUES = frozenset(
    {
        "the", "a", "an", "this", "that", "these", "those", "my", "your", "its",
        "their", "our", "each", "every", "all", "any", "some", "no", "for", "of",
        "on", "in", "across", "with", "without", "from", "into", "over", "under",
        "about", "around", "between", "among", "per", "to", "at", "by",
    }
)
#: Words that follow a finite past tense but not an adjective, so an "-ed"
#: word straight after the verb and before one of these is a past tense.
_ACK_AFTER_FINITE = frozenset(
    {
        "on", "in", "at", "for", "with", "after", "because", "due", "to", "from",
        "again", "twice", "overnight", "earlier", "today", "yesterday",
        "successfully", "cleanly", "back", "up", "down", "out", "and", "but",
        "without",
    }
)
#: Adverbs ending "-ly" follow a finite past tense ("upgrading failed
#: silently.").
_ACK_ADVERB_SUFFIX = "ly"
#: Determiners and pronouns that open an object. After the hand-off verb or
#: one of ``_ACK_OBJECT_LEADS`` they belong to the target; after any other
#: word, that word is a verb taking them.
_ACK_OBJECTS = frozenset(
    {
        "the", "a", "an", "it", "them", "this", "these", "those", "my", "your",
        "our", "its", "their", "every", "each", "everything", "nothing",
        "something",
    }
)
_ACK_OBJECT_LEADS = _ACK_ADJECTIVE_CUES | {
    "and", "or", "but", "back", "up", "down", "out", "off", "through",
}
#: Words that join the items of a list, so a clause after the last comma
#: holding one is the list's tail rather than a sentence of its own.
_ACK_LIST_JOINERS = frozenset({"and", "or", "&"})
_ACK_MIN_CLAUSE_WORDS = 3
_ACK_QUESTION_MARKS = ("?", "!")
_ACK_NOUN_SUFFIX = "eed"
_ACK_NAME_CHARACTERS = "-0123456789"
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
_ACK_CURLY_APOSTROPHE = "\u2019"


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


def _is_past_tense(word: str) -> bool:
    return (
        word.endswith(_ACK_PAST_TENSE)
        and not word.endswith(_ACK_NOUN_SUFFIX)
        and not any(character in word for character in _ACK_NAME_CHARACTERS)
    )


def _is_adjective(bare: list[str], index: int) -> bool:
    """Whether the "-ed" word at ``index`` modifies a noun in the target."""

    if bare[index - 1] in _ACK_ADJECTIVE_CUES:
        return True
    following = bare[index + 1] if index + 1 < len(bare) else ""
    return (
        index == 1
        and bool(following)
        and following not in _ACK_AFTER_FINITE
        and not following.endswith(_ACK_ADVERB_SUFFIX)
    )


def _is_progress_ack(sentence: str) -> bool:
    """Whether ``sentence`` is the one-line hand-off, per the comment on
    ``_DELEGATION_ACK``."""

    text = sentence.strip()
    if "\n" in text:
        return False
    if text.rstrip(_ACK_WORD_WRAPPING).endswith(_ACK_QUESTION_MARKS):
        return False
    body = _ACK_CLOSING.sub("", text)
    words = body.split()
    if not 2 <= len(words) <= _ACK_MAX_TARGET_WORDS + 1:
        return False
    bare = [
        word.replace(_ACK_CURLY_APOSTROPHE, "'")
        .strip(_ACK_WORD_WRAPPING)
        .rstrip(",")
        .casefold()
        for word in words
    ]
    if bare[0] not in _ACK_VERBS:
        return False
    if any(";" in word or word.endswith(":") for word in bare):
        return False
    for index, word in enumerate(bare[1:], start=1):
        if word in _ACK_CLAUSE_OPENERS:
            break
        if word in _ACK_VERDICTS or word in _ACK_FINDING_VERBS:
            return False
        if _is_past_tense(word) and not _is_adjective(bare, index):
            return False
        if (
            word in _ACK_OBJECTS
            and index > 1
            and bare[index - 1] not in _ACK_OBJECT_LEADS
        ):
            return False
    if "," in body:
        after = [
            word.strip(_ACK_WORD_WRAPPING).casefold()
            for word in body.rsplit(",", 1)[1].split()
        ]
        if after and after[0] in _ACK_CLAUSE_SUBJECTS:
            return False
        if any(_is_past_tense(word) or word[:1].isdigit() for word in after):
            return False
        if len(after) >= _ACK_MIN_CLAUSE_WORDS and not _ACK_LIST_JOINERS & set(
            after
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
        # A hand-off-shaped line before a question is what the question asks
        # about: "deleting cluster A. Confirm?" keeps the target.
        asks = bool(remainder) and remainder[-1].rstrip(
            _ACK_WORD_WRAPPING
        ).endswith(_ACK_QUESTION_MARKS)
        while remainder and (
            _DELEGATION_ACK.search(remainder[0])
            or (not asks and _is_progress_ack(remainder[0]))
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
