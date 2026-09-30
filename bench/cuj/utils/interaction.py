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
#: The template fixes the shape: one lowercase line, a hand-off verb, the
#: target, a period. ``_is_progress_ack`` strips a line only when every word
#: of it fits that shape and keeps it otherwise, because a kept ack is text a
#: reviewer reads in the transcript while a stripped answer is gone. The
#: verb comes from ``_ACK_VERBS`` in lowercase ("Checking the logs showed a
#: crash." is an answer); a closing period or ellipsis is optional; the target
#: runs to ``_ACK_MAX_TARGET_WORDS`` words. Past the verb, a word belongs to
#: the target when it is:
#:
#: - a name: a word holding one of ``_ACK_NAME_CHARACTERS`` or wrapped in
#:   backticks ("checkout-gateway", "us-central1", "app=web");
#: - a function word from ``_ACK_OBJECT_LEADS``, or a determiner or pronoun
#:   from ``_ACK_OBJECTS`` straight after the verb or one of those ("looking
#:   at the logs.", "checking all the nodes.");
#: - a naming participle from ``_ACK_NAMING_PARTICIPLES`` and the one word it
#:   names ("checking pods labeled app=web.");
#: - an "-ed" word after a determiner or preposition from
#:   ``_ACK_ADJECTIVE_CUES`` ("reviewing the failed rollout."),
#:   or straight after the verb when a plain word follows it ("reviewing
#:   failed rollouts in prod-a.");
#: - any other plain word, in a run of at most ``_ACK_MAX_PLAIN_RUN`` of
#:   them, that is not a verdict or a verb from ``_ACK_FINDING_VERBS``; a
#:   plain word straight after a name counts only when one of
#:   ``_ACK_OBJECTS`` came before the name ("reviewing the checkout-gateway
#:   rollout." is a target; "upgrading prod-a blocks on quota." and "checking
#:   quota in us-central1 hit limits." are not);
#: - after a clause opener straight after the verb, anything ("checking why
#:   the rollout failed.").
#:
#: Anything else keeps the line: a comma, semicolon, colon outside a link,
#: dash, question or exclamation mark ("checking the rollout, it's stuck."); a contraction of a
#: pronoun ("checking it's stuck."); a past tense elsewhere ("restarting
#: fixed checkout-gateway.", "provisioning failed when quota ran out."); a
#: determiner after a plain word ("restarting the pod cleared it."). The one
#: shape it cannot tell apart is a verb outside the lists inside a short run
#: of plain words: "scaling staging broke." has the words of "auditing
#: version skew." and is stripped, pinned in ``test_known_misreads_are_pinned``.
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
#: Participles that introduce a target's name after its noun.
_ACK_NAMING_PARTICIPLES = frozenset({"named", "called", "labeled", "labelled", "tagged"})
#: The most plain words in a row a target holds between names and function
#: words. "auditing unused node pools." is three.
_ACK_MAX_PLAIN_RUN = 3
#: Characters a hand-off's one line never holds.
_ACK_BREAKS = frozenset(",;:\u2014\u2013")
#: A Markdown link's URL, whose colon is not a clause break.
_ACK_LINK_TARGET = re.compile(r"\]\([^)\s]*\)")
#: Pronouns whose contraction ("it's", "there's") opens a clause of its own.
_ACK_CONTRACTED_SUBJECTS = frozenset(
    {"it", "there", "that", "what", "here", "they", "we", "i", "you", "he", "she", "who"}
)
_ACK_QUESTION_MARKS = ("?", "!")
_ACK_NOUN_SUFFIX = "eed"
_ACK_NAME_CHARACTERS = "-0123456789./_=@#"
_ACK_CLOSING = re.compile(r"(?:\.{1,3}|…)\Z")
_ACK_WORD_WRAPPING = "*_`[]()\"'"
_ACK_CODE = "`"
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


def _is_name(word: str) -> bool:
    return word.startswith(_ACK_CODE) or any(
        character in word.strip(_ACK_WORD_WRAPPING) for character in _ACK_NAME_CHARACTERS
    )


def _is_past_tense(word: str) -> bool:
    return word.endswith(_ACK_PAST_TENSE) and not word.endswith(_ACK_NOUN_SUFFIX)


def _is_progress_ack(sentence: str) -> bool:
    """Whether ``sentence`` is the one-line hand-off, per the comment on
    ``_DELEGATION_ACK``: every word fits the template's shape."""

    text = sentence.strip()
    if "\n" in text or _ACK_BREAKS & set(_ACK_LINK_TARGET.sub("", text)):
        return False
    if text.rstrip(_ACK_WORD_WRAPPING).endswith(_ACK_QUESTION_MARKS):
        return False
    words = _ACK_CLOSING.sub("", text).split()
    if not 2 <= len(words) <= _ACK_MAX_TARGET_WORDS + 1:
        return False
    lead = words[0].strip(_ACK_WORD_WRAPPING)
    if lead not in _ACK_VERBS:
        return False
    names = [_is_name(word) for word in words]
    bare = [
        word.replace(_ACK_CURLY_APOSTROPHE, "'").strip(_ACK_WORD_WRAPPING).casefold()
        for word in words
    ]
    if bare[1] in _ACK_CLAUSE_OPENERS:
        return True
    run = 0
    introduced = True
    naming = False
    for index in range(1, len(bare)):
        word, previous = bare[index], bare[index - 1]
        following = bare[index + 1] if index + 1 < len(bare) else ""
        if naming:
            naming = False
            continue
        if names[index]:
            run = 0
            introduced = previous in _ACK_OBJECTS
            continue
        if "'" in word and word.split("'", 1)[0] in _ACK_CONTRACTED_SUBJECTS:
            return False
        if word in _ACK_OBJECTS:
            if index > 1 and previous not in _ACK_OBJECT_LEADS and previous not in _ACK_OBJECTS:
                return False
            run, introduced = 0, True
            continue
        if word in _ACK_OBJECT_LEADS:
            run, introduced = 0, True
            continue
        if word in _ACK_NAMING_PARTICIPLES and index > 1:
            naming = True
            continue
        if word in _ACK_VERDICTS or word in _ACK_FINDING_VERBS or word in _ACK_CLAUSE_OPENERS:
            return False
        if _is_past_tense(word):
            adjective = previous in _ACK_ADJECTIVE_CUES or (
                index == 1
                and bool(following)
                and not names[index + 1]
                and following not in _ACK_AFTER_FINITE
                and following not in _ACK_OBJECTS
                and following not in _ACK_CLAUSE_OPENERS
                and not following.endswith(_ACK_ADVERB_SUFFIX)
            )
            if not adjective:
                return False
        if index > 1 and names[index - 1] and not introduced:
            return False
        run += 1
        if run > _ACK_MAX_PLAIN_RUN:
            return False
    return not naming


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
