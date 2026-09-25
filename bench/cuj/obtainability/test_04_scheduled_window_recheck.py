"""Reproduce CUJ4: the scheduled trigger path of the obtainability capability.

CUJ3 (test_03) grades the chat-triggered window plan. This journey grades the
capability delivery vehicle's scheduled path for the same capability
(docs/designs/capability-delivery-vehicle.md, R2): an advisory capability is
asked for rather than scheduled, and gets a schedule only as a re-check the
user opts into, reporting into the conversation that asked. The test acts as
that user: it asks for a window plan plus a one-time re-check shortly before
the recommended window, and scores only what the portal returns — the
delivered commitment: a fire time exactly the lead before a stated start,
the destination, a single firing. The tool-evidence proof of the created
schedule is a diagnostic milestone, not an acceptance criterion, until the
portal projects worker tool calls with their actions: tool names alone
cannot tell a cron create from a cron list.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from cuj.utils.acceptance_criteria import AcceptanceCriteria, AcceptanceCriterion
from cuj.utils.interaction import (
    delivered_answer,
    projected_tasks,
    tool_operations,
    unnormalized_tool_calls,
)
from cuj.utils.milestones import Milestone, MilestoneSuite
from cuj.utils.scenario import Scenario

RECHECK_LEAD = timedelta(hours=2)
RECHECK_LEAD_HOURS = int(RECHECK_LEAD.total_seconds() // 3600)
RECHECK_LEAD_MINUTES = int(RECHECK_LEAD.total_seconds() // 60)
# The lead spelled both ways an answer may echo it. The digit form rejects
# a preceding digit: "12 hours" is the job's duration, not the lead.
LEAD_RE = re.compile(rf"(?<!\d)(?:{RECHECK_LEAD_HOURS}|two)\s+hours?\b")
REQUIRED_SKILLS = {"capacity-obtainability"}
# The one scheduling tool the runtime exposes. The projection carries tool
# names without arguments, so a match shows scheduling activity, not a
# create; the milestone that reads this says so, and the acceptance
# criterion it replaced returns when worker tool calls project with their
# actions.
SCHEDULE_OPERATIONS = {"cronjob"}
FORBIDDEN_OPERATIONS = {
    "apply_manifest",
    "create_cluster",
    "open_pull_request",
    "submit_provisioning_request",
    "submit_suggestion",
}
# One UTC clock time in the delivered answer: "22:00 UTC", "9:30 UTC", or
# inside an ISO-8601 timestamp ("2026-09-22T22:00:00Z"). No leading word
# boundary — a digit inside a timestamp has none.
UTC_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})")
# The one-shot verdict is judged per recurrence mention: a recurrence word
# counts against the answer only in a sentence about the schedule, and a
# negation neutralizes it only when it sits just before it ("never a
# recurring schedule") — a "no" elsewhere in the sentence ("with no end
# date") does not un-say "hourly". Bare "once" is more often temporal
# ("once the window is confirmed") and bare "single" descriptive ("the
# single best zone"), so only firing-count shapes commit.
ONE_SHOT_RE = re.compile(
    r"\b(exactly once|only once|fires? once|runs? once|one[- ]time"
    r"|single\s+(?:re-?check|firing|run|execution))\b"
)
NEGATION_WINDOW_CHARS = 20
SCHEDULE_TERM_RE = re.compile(r"\b(re-?check|schedul\w*|cron\w*|fires?|firing)\b")
RECURRENCE_RE = re.compile(
    r"\b(recurring|repeat\w*|hourly|daily|weekly|nightly"
    r"|every\s+\d+\s*(?:minutes?|hours?|days?|weeks?)"
    r"|every\s+(?:minute|hour|day|week|night|morning))\b"
)
NEGATION_RE = re.compile(
    r"\b(non|not|never|no|won'?t|will not|isn'?t|is not|rather than|instead of)\b"
)
SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


# An RFC 3339 timestamp with a Z suffix IS a UTC statement — the same rule
# test_03 and test_05 apply to their start times.
ZULU_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z")


def _clock_minutes(text: str) -> set[int]:
    return {
        int(hour) * 60 + int(minute)
        for hour, minute in UTC_TIME_RE.findall(text)
        if int(hour) < 24 and int(minute) < 60
    }

PROMPT = f"""Act as my batch scheduling partner. Plan a 64-node TPU v5e \
training job that must run for 12 hours and finish within the next 48 hours, \
evaluating us-central1-a and europe-west4-b with live \
compute.beta.AdviceService.CalendarMode evidence, and recommend an exact UTC \
start time and zone. Then schedule a single re-check of the recommended \
window {RECHECK_LEAD_HOURS} hours before its start: the re-check re-probes \
the window, reports into this conversation, and must fire exactly once — \
never a recurring schedule. Tell me the re-check's exact UTC fire time and \
where its report will arrive. This is planning-only: do not apply manifests, \
submit a provisioning request, create infrastructure, or mutate Kubernetes \
or cloud state."""

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-recheck-request-preserved",
        "The request opts into a one-time re-check two hours before the "
        "recommended window, reporting into the same conversation.",
        "projected user input carries the plan and the re-check opt-in",
    ),
    AcceptanceCriterion(
        "ac03-recheck-fire-time-stated",
        "The user is told the re-check's exact UTC fire time, two hours "
        "before the recommended window's start.",
        "the answer names the lead and two UTC times exactly two hours "
        "apart, the fire time in a sentence about the schedule",
    ),
    AcceptanceCriterion(
        "ac04-recheck-reports-to-thread",
        "The user is told the re-check reports into this conversation, not "
        "into a separate session.",
        "the delivered answer names this conversation as the destination",
    ),
    AcceptanceCriterion(
        "ac05-recheck-is-one-shot",
        "The re-check fires once: the answer states the firing count, "
        "and no schedule sentence carries an un-negated recurrence.",
        "the delivered answer commits to a single firing",
    ),
)

MILESTONES = (
    Milestone(
        "interaction-completed",
        "The plan-plus-re-check conversation finishes successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        "m1-kage-delegates",
        "Kage delegates the specialist planning request.",
        "root toolCalls contains a completed kanban_create",
        ("interaction-completed",),
    ),
    Milestone(
        "m2-obtainability-skill-loaded",
        "The delegated task loads the obtainability capability's skill.",
        "the platform task loads capacity-obtainability",
        ("m1-kage-delegates",),
    ),
    Milestone(
        "m3-planning-remains-read-only",
        "Scheduling the re-check mutates nothing beyond the schedule itself.",
        "complete normalized tool evidence contains no forbidden mutation",
        ("interaction-completed",),
    ),
    Milestone(
        "m4-recheck-schedule-observed",
        "The re-check left tool evidence: a completed cronjob call in the "
        "interaction. Names only — the projection carries no arguments, so "
        "this cannot tell a create from a list.",
        "completed tool operations include cronjob",
        ("interaction-completed",),
    ),
)


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    interaction_blocker = (
        ()
        if interaction.get("status") == "completed"
        and interaction.get("terminal") is True
        else ("interaction did not complete successfully",)
    )
    final_output_blocker = (
        ()
        if "output" in interaction
        else ("portal interaction projection omits output",)
    )

    answer = delivered_answer(interaction)
    folded_answer = answer.casefold()
    # A fire time cannot be told apart from the plan's own start by its
    # presence alone — every correct plan states a start, and the report
    # format supplies more clock times (runner-up windows, the deadline,
    # echoed probe ranges), any pair of which could sit two hours apart by
    # coincidence. What only a stated fire time produces is a clock time in
    # a sentence about the schedule that sits exactly the lead before some
    # stated time.
    all_clocks = _clock_minutes(answer)
    recheck_clocks = {
        clock
        for sentence in SENTENCE_SPLIT_RE.split(folded_answer)
        if SCHEDULE_TERM_RE.search(sentence)
        for clock in _clock_minutes(sentence)
    }
    fire_time_stated = (
        any(
            (start - fire) % (24 * 60) == RECHECK_LEAD_MINUTES
            for start in all_clocks
            for fire in recheck_clocks
        )
        and ("utc" in folded_answer or bool(ZULU_RE.search(answer)))
        and bool(LEAD_RE.search(folded_answer))
    )
    reports_to_thread = (
        "this conversation" in folded_answer
        or "this thread" in folded_answer
        or "same conversation" in folded_answer
        or "same thread" in folded_answer
    )
    recurring_committed = any(
        SCHEDULE_TERM_RE.search(sentence)
        and not NEGATION_RE.search(
            sentence[max(0, match.start() - NEGATION_WINDOW_CHARS) : match.start()]
        )
        for sentence in SENTENCE_SPLIT_RE.split(folded_answer)
        for match in RECURRENCE_RE.finditer(sentence)
    )
    one_shot = bool(ONE_SHOT_RE.search(folded_answer)) and not recurring_committed

    input_value = interaction.get("input")
    input_text = str(
        (input_value or {}).get("text") if isinstance(input_value, dict) else ""
    )
    folded_input = input_text.casefold()

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-recheck-request-preserved",
        "64-node" in folded_input
        and "tpu v5e" in folded_input
        and "re-check" in folded_input
        and f"{RECHECK_LEAD_HOURS} hours before" in folded_input
        and "this conversation" in folded_input
        and "exactly once" in folded_input,
        input_text,
    )
    suite.record(
        "ac03-recheck-fire-time-stated",
        fire_time_stated,
        answer,
        blocked_by=tuple(
            dict.fromkeys((*interaction_blocker, *final_output_blocker))
        ),
    )
    suite.record(
        "ac04-recheck-reports-to-thread",
        reports_to_thread,
        answer,
        blocked_by=tuple(
            dict.fromkeys((*interaction_blocker, *final_output_blocker))
        ),
    )
    suite.record(
        "ac05-recheck-is-one-shot",
        one_shot,
        answer,
        blocked_by=tuple(
            dict.fromkeys((*interaction_blocker, *final_output_blocker))
        ),
    )
    return suite


def evaluate_kage_milestones(interaction: dict[str, Any]) -> MilestoneSuite:
    platform_tasks = projected_tasks(interaction, assignee="platform")
    operations = tool_operations(interaction)
    completed_operations = tool_operations(interaction, completed_only=True)
    worker_tools_available = bool(platform_tasks) and all(
        "toolCalls" in task for task in platform_tasks
    )
    unnormalized_calls = unnormalized_tool_calls(interaction)
    routed = [
        task
        for task in platform_tasks
        if REQUIRED_SKILLS <= set(task.get("skills") or [])
        and REQUIRED_SKILLS <= set(task.get("loadedSkills") or [])
    ]
    suite = MilestoneSuite(MILESTONES)
    suite.record(
        "interaction-completed",
        interaction.get("status") == "completed"
        and interaction.get("terminal") is True,
        interaction.get("status"),
    )
    suite.record(
        "m1-kage-delegates",
        "kanban_create" in completed_operations,
        completed_operations,
    )
    suite.record(
        "m2-obtainability-skill-loaded",
        bool(routed),
        [
            {
                "taskId": task.get("taskId"),
                "skills": task.get("skills"),
                "loadedSkills": task.get("loadedSkills"),
            }
            for task in platform_tasks
        ],
        blocked_by=()
        if any("skills" in task and "loadedSkills" in task for task in platform_tasks)
        else ("portal task projection omits skills/loadedSkills",),
    )
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    suite.record(
        "m3-planning-remains-read-only",
        interaction.get("toolEvidenceComplete") is True
        and not mutations
        and not unnormalized_calls,
        {
            "toolEvidenceComplete": interaction.get("toolEvidenceComplete"),
            "operations": operations,
            "mutations": mutations,
            "unnormalizedTools": unnormalized_calls,
        },
        # An observed mutation outranks any missing-evidence reason.
        blocked_by=()
        if mutations
        else tuple(
            reason
            for condition, reason in (
                (
                    "toolEvidenceComplete" not in interaction,
                    "portal interaction projection omits toolEvidenceComplete",
                ),
                (
                    not worker_tools_available,
                    "portal task projection omits worker toolCalls",
                ),
                (
                    bool(unnormalized_calls),
                    "tool evidence omits normalized operations",
                ),
            )
            if condition
        ),
    )
    suite.record(
        "m4-recheck-schedule-observed",
        interaction.get("toolEvidenceComplete") is True
        and bool(SCHEDULE_OPERATIONS.intersection(completed_operations)),
        {"completedOperations": sorted(completed_operations)},
        blocked_by=()
        if "toolEvidenceComplete" in interaction
        else ("portal interaction projection omits toolEvidenceComplete",),
    )
    return suite


def build_prompt() -> str:
    return PROMPT


def test_04_scheduled_window_recheck() -> None:
    Scenario(
        "cuj4",
        build_prompt,
        evaluate_acceptance,
        evaluate_kage_milestones,
    ).run_test()
