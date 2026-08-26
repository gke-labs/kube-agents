"""Unit tests for the kanban notifier patch installed by deploy/docker/Dockerfile.

Merges what were ``test_kanban_wake_kinds.py`` and ``test_kanban_result_delivery.py``,
and adds :class:`LegacyEquivalenceTest`, which pins the claim the merge rests on:
one applier with two anchors produces the same patched source as the three it
replaced. ``test_kanban_handoff_clip.py`` stays separate — it tests the shared
text utility, which has no anchor into upstream source.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from apply_kanban_notifier import (
    HANDOFF_ANCHOR,
    INCIDENT_CALL,
    MARKER_CALL,
    RELATIVE,
    WAKE_ANCHOR,
    apply,
)
from apply_kanban_progress_lines import SEND_ANCHOR, SEND_PATCHED
from kanban_handoff_clip import DEFAULT_LIMIT, ELLIPSIS, clip_handoff
from kanban_notifier import (
    DEFAULT_WAKE_KINDS,
    MAX_NOTES,
    NOTE_SIGNATURE,
    RESULT_LIMIT,
    SEPARATOR,
    UNSTRUCTURED_MIN_CHARS,
    _warned_config,
    actionable_report,
    completion_note,
    creator_session_key,
    handoff_with_result,
    note_suppressed_completion,
    resolve_wake_kinds,
    result_block,
    store_incident_report,
    suppressed_kinds,
    unstructured_result,
    wake_kinds_for,
)

#: Read as text rather than imported: ``verify_kanban_notifier.py`` runs at the
#: top level against a patched ``/opt/hermes`` and cannot be imported here.
VERIFIER_SOURCE = (Path(__file__).parent / "verify_kanban_notifier.py").read_text()

# The status line the incident actually delivered, and the catalogue it should
# have carried with it.
INCIDENT_SUMMARY = (
    "Successfully inspected and cataloged all 9 active platform-agent-level and "
    "system-wide cron jobs. Compiled their detailed purposes, schedules, and "
    "active configurations."
)
INCIDENT_RESULT = "\n".join(
    f"{i}. cron-job-{i} — schedule `0 {i} * * *` — enabled" for i in range(1, 10)
)

# What agents/chat/config.yaml sets: wake the front door when a card fails,
# never when it succeeds — the notifier has already delivered that summary.
FAILURE_ONLY = ["gave_up", "crashed", "timed_out", "blocked"]


class _Task:
    def __init__(self, result=None):
        self.result = result


def loader(kanban=None, raises=False, not_a_dict=False):
    """Build a load_config callable for a given kanban config subtree."""

    def _load():
        if raises:
            raise RuntimeError("config unreadable")
        if not_a_dict:
            return "not a mapping"
        return {"kanban": kanban} if kanban is not None else {}

    return _load


class Event:
    def __init__(self, kind):
        self.kind = kind


# =============================================================================
# Delivery
# =============================================================================


class ResultBlockTest(unittest.TestCase):
    def test_the_incident_catalogue_is_delivered(self):
        block = result_block(INCIDENT_SUMMARY, INCIDENT_RESULT)
        self.assertTrue(block.startswith(SEPARATOR))
        self.assertIn("cron-job-1", block)
        self.assertIn("cron-job-9", block)
        # Nothing was lost: every line of the catalogue is present.
        for line in INCIDENT_RESULT.splitlines():
            self.assertIn(line, block)

    def test_multi_line_results_survive_whole(self):
        # The failure the summary channel cannot avoid: it keeps only line one.
        self.assertEqual(len(INCIDENT_RESULT.splitlines()), 9)
        block = result_block("status", INCIDENT_RESULT)
        self.assertEqual(len(block.strip().splitlines()), 9)

    def test_an_empty_result_delivers_nothing(self):
        for empty in (None, "", "   ", "\n\t "):
            self.assertEqual(result_block(INCIDENT_SUMMARY, empty), "")

    def test_a_result_already_in_the_status_line_is_not_repeated(self):
        # What happens when a worker puts one body of text in both fields, and
        # when the require-result gate promotes summary into result.
        self.assertEqual(result_block(INCIDENT_SUMMARY, INCIDENT_SUMMARY), "")

    def test_dedup_ignores_whitespace_and_case(self):
        delivered = "Restarted the deployment; 3/3 pods ready"
        self.assertEqual(result_block(delivered, "restarted  the\ndeployment;   3/3 PODS ready"), "")

    def test_a_longer_result_is_delivered_even_if_it_starts_the_same(self):
        delivered = "Found 9 jobs."
        block = result_block(delivered, "Found 9 jobs.\n\n" + INCIDENT_RESULT)
        self.assertIn("cron-job-5", block)

    def test_no_status_line_still_delivers(self):
        for delivered in (None, ""):
            self.assertIn("cron-job-1", result_block(delivered, INCIDENT_RESULT))

    def test_a_runaway_result_is_clipped_and_says_so(self):
        huge = " ".join(f"token{i}" for i in range(20000))
        self.assertGreater(len(huge), RESULT_LIMIT)
        block = result_block("status", huge)
        self.assertIn("clipped", block.lower())
        self.assertIn(str(RESULT_LIMIT), block)

    def test_a_result_at_the_limit_is_not_marked_clipped(self):
        body = "x" * 100
        block = result_block("status", body)
        self.assertNotIn("clipped", block.lower())
        self.assertEqual(block, SEPARATOR + body)

    def test_clipping_never_severs_a_url(self):
        url = "https://github.com/gke-agentic/adamparco-infra/issues/30"
        body = " ".join(f"token{i}" for i in range(20000)) + " " + url
        block = result_block("status", body, limit=200)
        # Either the whole link or none of it — never a prefix that 404s.
        self.assertNotIn("https://github.com/gke-agentic/adamparco-infra/is\n", block)
        self.assertEqual(clip_handoff(body, 200), block[len(SEPARATOR):].split("\n\n[")[0])

    def test_the_budget_leaves_room_under_the_slack_ceiling(self):
        # Slack's adapter chunks at MAX_MESSAGE_LENGTH = 39000. The status line
        # (<=1200), the title (<=120), and the clip marker must all fit too.
        self.assertLess(RESULT_LIMIT + 1200 + 120 + len("[Result clipped at 30000 characters]"), 39000)


def notifier_tail(payload_summary, task):
    """Build the completion message's tail the way the patched notifier does.

    The three lines before the call are copied from the ``completed`` branch of
    ``gateway/kanban_watchers.py`` — see UPSTREAM_WATCHERS below, which carries
    the same code at its real indentation. They matter to these tests because
    the ``elif`` is where ``handoff`` becomes a clip of the very field this
    module delivers, and testing ``handoff_with_result`` on a status line
    invented by the test would miss that entirely.
    """
    handoff = ""
    if payload_summary:
        handoff = f"\n{clip_handoff(payload_summary)}"
    elif task and task.result:
        handoff = f"\n{clip_handoff(task.result)}"
    return handoff_with_result(handoff, task)


def report_of_length(length):
    """A plausible multi-line report of exactly ``length`` characters.

    Real text with whitespace in it, because ``clip_handoff`` cuts on a token
    boundary: a run of one repeated character would take the no-whitespace
    branch and clip somewhere these tests do not mean to exercise.
    """
    lines = []
    while len("\n".join(lines)) < length:
        i = len(lines) + 1
        lines.append(f"{i}. cron-job-{i} — schedule `0 {i} * * *` — enabled")
    body = "\n".join(lines)[:length]
    # An exact cut can land on the newline between two lines, and ``result``
    # is stripped before it is measured, which would put the body one short of
    # the boundary the test is aiming at.
    return body[:-1] + "." if body[-1].isspace() else body


class HandoffWithResultTest(unittest.TestCase):
    def test_a_missing_task_row_leaves_the_status_line_alone(self):
        self.assertEqual(handoff_with_result("\nstatus", None), "\nstatus")

    def test_a_task_without_a_result_attribute_leaves_the_status_line_alone(self):
        self.assertEqual(handoff_with_result("\nstatus", object()), "\nstatus")

    def test_a_raising_task_row_cannot_wedge_the_notifier(self):
        class Exploding:
            @property
            def result(self):
                raise RuntimeError("boom")

        self.assertEqual(handoff_with_result("\nstatus", Exploding()), "\nstatus")

    def test_an_absent_handoff_still_delivers_the_report(self):
        for delivered in (None, ""):
            self.assertIn("cron-job-1", handoff_with_result(delivered, _Task(INCIDENT_RESULT)))

    def test_the_summary_branch_keeps_the_status_line_and_adds_the_report(self):
        tail = notifier_tail(INCIDENT_SUMMARY, _Task(INCIDENT_RESULT))
        self.assertIn(INCIDENT_SUMMARY, tail)
        self.assertEqual(tail.count(INCIDENT_RESULT), 1)


#: Card ``t_3ba2166a`` as it actually closed on 2026-08-09: a report that meant
#: to have sections and expressed every one of them in a way Slack cannot see.
#: Block Kit renders it as three blocks — two ``section``s and one
#: undifferentiated ``rich_text`` list — against seven for the abridged
#: Markdown below, which keeps three ``header``s, a ``table`` and a ``divider``.
FLAT_RESULT = """=== Wall-Clock Delay Synthesis Report ===

Here is the high-quality analysis of the scheduling latency, active execution
time, and total wall-clock delay for the orchestrated sleep tasks.

1. TIMELINE BREAKDOWN
* Start Epoch (User's Initial Message): 1786236658.839329
* Parent Tasks Claimed/Started:
  - Sleep Task 1 (t_2b8c6e73): 1786236718 (59.16 seconds after start)
  - Sleep Task 2 (t_5372e0ed): 1786236719 (60.16 seconds after start)
  - Sleep Task 3 (t_557a2a6a): 1786236720 (61.16 seconds after start)

2. WALL-CLOCK DELAY CALCULATION
* Formula: End Epoch - Start Epoch
* Total Wall-Clock Delay: 125.798794 seconds (approx 2 minutes, 5.8 seconds)

3. ACTIVE EXECUTION TIME VS. SCHEDULING LATENCY
* Total Active Execution Time (Container Runtimes): 66.638123 seconds
  - Concurrent Sleep Phase: 24.000000 seconds
  - Synthesis Phase: 42.638123 seconds
"""

#: The same report, written the way the persona contract now asks for. Opens at
#: ``##`` and not ``#``: an H1 is a ``top-level-heading`` defect, which is the
#: tier this module warns about, so a fixture named for the well-shaped case
#: must not carry one. ``unstructured_result`` does not look at headings, which
#: is why the H1 this used to open with went unnoticed here.
STRUCTURED_RESULT = """## Wall-Clock Delay Synthesis Report

Analysis of scheduling latency, active execution time and total wall-clock delay.

## Timeline

| Event | Epoch | Offset |
| --- | ---: | ---: |
| Start (user message) | 1786236658.839 | 0.00s |
| Sleep Task 1 claimed | 1786236718 | 59.16s |

---

## Wall-clock delay

- **Formula:** End Epoch - Start Epoch
- **Total:** 125.798794 seconds (approx 2 minutes, 5.8 seconds)
"""


class UnstructuredResultTest(unittest.TestCase):
    """The observation that card ``t_3ba2166a`` would render flat.

    Log-only, so these pin the predicate rather than any change to the message.
    """

    def test_the_card_that_motivated_this_is_flagged(self):
        self.assertTrue(unstructured_result(FLAT_RESULT))

    def test_the_same_report_in_markdown_is_not(self):
        self.assertFalse(unstructured_result(STRUCTURED_RESULT))

    def test_a_short_answer_stays_quiet(self):
        self.assertFalse(unstructured_result("=== Done ===\n1. ALL GOOD"))

    def test_the_floor_is_low_enough_to_see_a_small_report(self):
        """``t_88cdceb1`` was 240 characters and ``t_c60439af`` 189.

        The floor was 600 until 2026-08-08, which put both of the cards that
        prompted this work permanently out of range. Anything at or above the
        floor must be measurable; this pins the floor itself, because raising it
        back over ~200 would silently restore the blind spot.
        """
        self.assertLessEqual(UNSTRUCTURED_MIN_CHARS, 189)
        small = "=== Timing Details ===\n" + ("value line here\n" * 12)
        self.assertGreaterEqual(len(small.strip()), UNSTRUCTURED_MIN_CHARS)
        self.assertTrue(unstructured_result(small))

    def test_long_prose_without_ascii_sections_stays_quiet(self):
        """A narrative answer has no structure to lose, so it is not a defect."""
        prose = ("No drift was found on any cluster in the fleet this morning. " * 20).strip()
        self.assertGreater(len(prose), UNSTRUCTURED_MIN_CHARS)
        self.assertFalse(unstructured_result(prose))

    def test_any_block_level_markdown_suppresses_it(self):
        for structure in ("## Section", "| a | b |", "---", "```py"):
            with self.subTest(structure=structure):
                self.assertFalse(unstructured_result(structure + "\n" + FLAT_RESULT))

    def test_a_missing_result_is_not_a_finding(self):
        for empty in (None, "", "   "):
            with self.subTest(result=empty):
                self.assertFalse(unstructured_result(empty))

    def test_delivery_logs_the_warning_but_sends_the_report_unchanged(self):
        with self.assertLogs("gateway.run", level="WARNING") as captured:
            tail = handoff_with_result("\nstatus", _Task(FLAT_RESULT))
        logged = "\n".join(captured.output)
        self.assertIn("will not render well in chat", logged)
        # The warning names the defect, so a log line is enough to tell which
        # rule fired without going back to the card.
        self.assertIn("ascii-substitute", logged)
        self.assertIn("WALL-CLOCK DELAY CALCULATION", tail)

    def test_the_warning_falls_back_when_the_shared_module_is_absent(self):
        """``gateway`` importing ``tools`` is optional, by construction.

        The richer defect list lives in ``tools/kanban_report_format.py`` and is
        imported lazily inside the warning. These tests run with no ``tools``
        package on the path at all, so this exercise *is* the fallback: the one
        defect this module can name on its own still reaches the log.
        """
        with self.assertLogs("gateway.run", level="WARNING") as captured:
            handoff_with_result("\nstatus", _Task(FLAT_RESULT))
        self.assertIn("ascii-substitute", "\n".join(captured.output))

    def test_a_structured_report_logs_nothing(self):
        logger = logging.getLogger("gateway.run")
        with self.assertNoLogs(logger, level="WARNING"):
            handoff_with_result("\nstatus", _Task(STRUCTURED_RESULT))

    def test_a_raising_result_cannot_wedge_the_delivery_path(self):
        class Exploding:
            @property
            def result(self):
                raise RuntimeError("boom")

        self.assertEqual(handoff_with_result("\nstatus", Exploding()), "\nstatus")


#: Card ``t_c781d6b0``, the sibling a reviewer read as fine: a ``###``, a lead
#: sentence, values in backticks. No defect at either level.
CLEAN_RESULT = """### Sleep Task 1 Completion

The requested sleep of 1 millisecond has been executed. Here are the recorded \
active execution details:

- **Start Unix Epoch:** `1786240527.916398`
- **End Unix Epoch:** `1786240527.9178874`
- **Elapsed Active Execution Time:** `0.001489400863647461` seconds"""

#: A heading over a bare list with raw floats: two defects, neither serious.
#: 189 characters, so comfortably over ``UNSTRUCTURED_MIN_CHARS``.
COSMETIC_RESULT = """### Sleep Task 3 Execution Details
- **Active Start (Unix Epoch):** 1786240531.1585038
- **Active End (Unix Epoch):** 1786240531.1598377
- **Active Duration:** 0.0013339519500732422 seconds"""


@contextlib.contextmanager
def _shared_defect_list_importable():
    """Make ``from tools.kanban_report_format import …`` resolve to the flat module.

    The suite deliberately runs with no ``tools`` package on the path, which is
    what exercises :func:`_log_result_shape`'s fallback. The two-level split
    only exists when the real defect list *is* importable, so this stitches the
    same file in under the name the notifier reaches for at runtime.
    """
    import kanban_report_format

    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    pkg.kanban_report_format = kanban_report_format
    with mock.patch.dict(
        sys.modules,
        {"tools": pkg, "tools.kanban_report_format": kanban_report_format},
    ):
        yield


class ResultShapeLogLevelTest(unittest.TestCase):
    """Two levels, because a log line that argues about taste gets ignored.

    The review finding this answers: the notifier warned on defects nobody needs
    waking for. The answer is not to stop measuring them — it is to say them at
    the level they deserve. Delivery is unaffected either way; by the time this
    runs the card has closed and the report is already on its way.
    """

    def test_a_serious_defect_warns_and_names_the_edit(self):
        with _shared_defect_list_importable():
            with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
                tail = handoff_with_result("\nstatus", _Task(FLAT_RESULT))
        logged = "\n".join(captured.output)
        self.assertIn("ascii-substitute", logged)
        # DEFECT_ADVICE, interpolated: the reader gets the fix, not a complaint.
        self.assertIn("pipe table", logged)
        self.assertIn("WALL-CLOCK DELAY CALCULATION", tail)

    def test_a_cosmetic_defect_does_not_warn(self):
        with _shared_defect_list_importable():
            with self.assertNoLogs("gateway.run", level=logging.WARNING):
                handoff_with_result("\nstatus", _Task(COSMETIC_RESULT))

    def test_a_cosmetic_defect_is_still_recorded_at_info(self):
        # Retiring these two defects was the other way to close the finding.
        # They stay measurable: a bad report read back later still shows why.
        with _shared_defect_list_importable():
            with self.assertLogs("gateway.run", level=logging.INFO) as captured:
                handoff_with_result("\nstatus", _Task(COSMETIC_RESULT))
        logged = "\n".join(captured.output)
        self.assertIn("heading-without-prose", logged)
        self.assertIn("unquoted-numerics", logged)
        self.assertIn("cosmetic", logged)

    def test_the_structured_fixture_never_warns(self):
        """Its raw epochs are a cosmetic defect, and that is the point.

        ``STRUCTURED_RESULT`` is a report a reviewer read as fine, and under the
        shared detector it still carries ``unquoted-numerics``. Warning on it
        would be exactly the noise the finding objected to.
        """
        with _shared_defect_list_importable():
            with self.assertNoLogs("gateway.run", level=logging.WARNING):
                handoff_with_result("\nstatus", _Task(STRUCTURED_RESULT))

    def test_a_clean_report_logs_nothing_at_all(self):
        with _shared_defect_list_importable():
            with self.assertNoLogs("gateway.run", level=logging.INFO):
                handoff_with_result("\nstatus", _Task(CLEAN_RESULT))

    def test_the_fallback_treats_its_one_defect_as_serious(self):
        """Without the shared list the notifier can only see ASCII substitutes.

        That one is in ``SERIOUS_DEFECTS``, so demoting the fallback to INFO
        would silently lose the only finding this module can make unaided.
        """
        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            handoff_with_result("\nstatus", _Task(FLAT_RESULT))
        self.assertIn("ascii-substitute", "\n".join(captured.output))

    def test_the_fallback_stays_quiet_on_a_cosmetic_defect(self):
        with self.assertNoLogs("gateway.run", level=logging.INFO):
            handoff_with_result("\nstatus", _Task(COSMETIC_RESULT))


class ClipBoundaryTest(unittest.TestCase):
    """The lengths at which the duplicate-delivery bug switched on.

    Under ``DEFAULT_LIMIT`` the notifier's status line is the whole report, the
    containment test in :func:`result_block` sees it, and nothing is appended.
    Over ``DEFAULT_LIMIT`` the status line is a clipped prefix — the report can
    no longer be found inside it, so the old ``handoff +=`` wiring sent the
    opening of the report and then the report. A 60-line cron catalogue arrived
    with jobs 1 to 19 printed twice.
    """

    LENGTHS = (DEFAULT_LIMIT - 1, DEFAULT_LIMIT, DEFAULT_LIMIT * 4)

    def assert_delivered_once(self, tail, body):
        opening = body.splitlines()[0]
        self.assertEqual(tail.count(body), 1, "the report itself is duplicated")
        self.assertEqual(
            tail.count(opening),
            1,
            "the report's opening lines are duplicated by the clipped status line",
        )

    def test_the_no_summary_branch_delivers_the_report_exactly_once(self):
        for length in self.LENGTHS:
            with self.subTest(length=length):
                body = report_of_length(length)
                self.assertEqual(len(body), length)
                self.assert_delivered_once(notifier_tail(None, _Task(body)), body)

    def test_the_no_summary_branch_drops_the_clip_marker_it_no_longer_needs(self):
        # Over budget the status line is discarded outright, so the reader
        # never sees a "[…]" promising more above text that is already whole.
        body = report_of_length(DEFAULT_LIMIT * 4)
        self.assertIn(ELLIPSIS, clip_handoff(body))
        self.assertNotIn(ELLIPSIS, notifier_tail(None, _Task(body)))

    def test_the_summary_branch_delivers_the_report_exactly_once(self):
        for length in self.LENGTHS:
            with self.subTest(length=length):
                body = report_of_length(length)
                tail = notifier_tail(INCIDENT_SUMMARY, _Task(body))
                self.assertIn(INCIDENT_SUMMARY, tail)
                self.assert_delivered_once(tail, body)

    def test_a_status_line_that_is_not_the_report_is_never_dropped(self):
        # The distinction the fix turns on: a clipped prefix of the report is
        # redundant, a summary that happens to be long is not.
        summary = report_of_length(DEFAULT_LIMIT * 2).replace("cron-job", "audit-step")
        body = report_of_length(DEFAULT_LIMIT * 4)
        tail = notifier_tail(summary, _Task(body))
        self.assertIn("audit-step-1 ", tail)
        self.assert_delivered_once(tail, body)


# =============================================================================
# The wake decision
# =============================================================================


class ResolveWakeKindsTest(unittest.TestCase):
    def setUp(self):
        # The degraded-read warnings are one-shot per process; without this
        # the first test to trip one hides it from every test after it.
        _warned_config.clear()

    def test_unset_key_keeps_upstream_behaviour(self):
        self.assertEqual(resolve_wake_kinds(loader({})), DEFAULT_WAKE_KINDS)
        self.assertEqual(resolve_wake_kinds(loader()), DEFAULT_WAKE_KINDS)

    def test_failure_only_config_drops_completed(self):
        kinds = resolve_wake_kinds(loader({"wake_on_events": FAILURE_ONLY}))
        self.assertNotIn("completed", kinds)
        self.assertEqual(set(kinds), set(FAILURE_ONLY))

    def test_explicit_empty_list_disables_the_wake(self):
        self.assertEqual(resolve_wake_kinds(loader({"wake_on_events": []})), ())

    def test_null_value_disables_the_wake(self):
        # `wake_on_events:` with nothing after it parses as None. Read that as
        # the override the user was clearly attempting, not as "unset".
        self.assertEqual(resolve_wake_kinds(loader({"wake_on_events": None})), ())

    def test_a_bare_string_is_accepted_as_one_kind(self):
        self.assertEqual(resolve_wake_kinds(loader({"wake_on_events": "crashed"})), ("crashed",))

    def test_unknown_kinds_are_dropped_and_logged(self):
        cfg = loader({"wake_on_events": ["crashed", "compleeted", "done"]})
        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            kinds = resolve_wake_kinds(cfg)
        self.assertEqual(kinds, ("crashed",))
        # The typo has to be visible: an unknown kind never matches a real
        # event, so silently dropping it looks like the wake breaking on its own.
        self.assertIn("compleeted", "\n".join(captured.output))

    def test_duplicates_collapse_and_order_is_preserved(self):
        kinds = resolve_wake_kinds(loader({"wake_on_events": ["blocked", "crashed", "blocked"]}))
        self.assertEqual(kinds, ("blocked", "crashed"))

    def test_wrong_shape_falls_back_to_the_default(self):
        with self.assertLogs("gateway.run", level=logging.WARNING):
            kinds = resolve_wake_kinds(loader({"wake_on_events": {"crashed": True}}))
        self.assertEqual(kinds, DEFAULT_WAKE_KINDS)

    def test_an_unreadable_config_still_wakes_on_failures(self):
        # Failing closed here would mean a crashed card silently never
        # escalating, which is worse than an extra turn on a healthy one.
        with self.assertLogs("gateway.run", level=logging.WARNING):
            self.assertEqual(resolve_wake_kinds(loader(raises=True)), DEFAULT_WAKE_KINDS)
        _warned_config.clear()
        with self.assertLogs("gateway.run", level=logging.WARNING):
            self.assertEqual(resolve_wake_kinds(loader(not_a_dict=True)), DEFAULT_WAKE_KINDS)

    def test_a_degraded_read_says_so_instead_of_looking_like_an_unset_key(self):
        # The failure this catches is silence, not breakage: falling back to
        # DEFAULT_WAKE_KINDS is byte-for-byte what an operator who never set
        # the key gets, so an unreadable config would present as "the narrowing
        # was never configured" while the redundant turn quietly came back.
        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            resolve_wake_kinds(loader(raises=True))
        joined = "\n".join(captured.output)
        self.assertIn("config unreadable", joined, "the cause must survive")
        self.assertIn("wake_on_events", joined, "the affected key must be named")

    def test_the_degraded_read_warning_does_not_repeat_every_delivery(self):
        # The notifier polls every 5s per subscription; an unconditional
        # warning here would be the loudest line in the gateway log.
        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            for _ in range(20):
                resolve_wake_kinds(loader(raises=True))
        self.assertEqual(len(captured.output), 1)

    def test_an_unset_key_is_not_a_warning(self):
        # The overwhelmingly common case. Warning on it would train operators
        # to ignore the line that matters.
        logging.getLogger("gateway.run").warning("probe")
        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            logging.getLogger("gateway.run").warning("probe")
            resolve_wake_kinds(loader({}))
        self.assertEqual(len(captured.output), 1)

    def test_default_set_matches_the_upstream_tuple(self):
        # If a base-image bump adds a terminal kind, this test is the reminder
        # to decide whether the front door should wake for it.
        self.assertEqual(
            DEFAULT_WAKE_KINDS,
            ("completed", "gave_up", "crashed", "timed_out", "blocked"),
        )


class WakeKindsForTest(unittest.TestCase):
    def test_matches_the_upstream_expression_by_default(self):
        events = [Event("completed"), Event("commented"), Event("crashed")]
        self.assertEqual(
            wake_kinds_for(events, loader({})),
            {"completed", "crashed"},
        )

    def test_completion_alone_produces_no_wake_under_failure_only(self):
        events = [Event("completed"), Event("commented")]
        self.assertEqual(wake_kinds_for(events, loader({"wake_on_events": FAILURE_ONLY})), set())

    def test_a_failure_in_the_same_batch_still_wakes(self):
        events = [Event("completed"), Event("timed_out")]
        self.assertEqual(
            wake_kinds_for(events, loader({"wake_on_events": FAILURE_ONLY})),
            {"timed_out"},
        )

    def test_events_without_a_kind_are_ignored(self):
        self.assertEqual(wake_kinds_for([object()], loader({})), set())


class Adapter:
    def __init__(self, supports_async_delivery):
        self.supports_async_delivery = supports_async_delivery


class NonPushAdapterTest(unittest.TestCase):
    """The narrowing applies to the push path only.

    Where the notifier skips its own ``send()`` it says the wake self-post IS
    the delivery, so a narrowed set there loses the result instead of saving a
    turn.
    """

    def test_a_non_push_adapter_still_wakes_on_completion(self):
        events = [Event("completed")]
        cfg = loader({"wake_on_events": FAILURE_ONLY})
        self.assertEqual(wake_kinds_for(events, cfg, adapter=Adapter(False)), {"completed"})

    def test_a_push_adapter_still_honours_the_narrowed_set(self):
        events = [Event("completed")]
        cfg = loader({"wake_on_events": FAILURE_ONLY})
        self.assertEqual(wake_kinds_for(events, cfg, adapter=Adapter(True)), set())

    def test_an_adapter_that_does_not_declare_the_flag_counts_as_push(self):
        # gateway.wake.adapter_supports_push defaults a missing attribute to
        # True; reading it as non-push would restore the redundant turn on
        # every Slack and Google Chat card.
        events = [Event("completed")]
        cfg = loader({"wake_on_events": FAILURE_ONLY})
        self.assertEqual(wake_kinds_for(events, cfg, adapter=object()), set())

    def test_an_explicit_empty_list_does_not_silence_the_non_push_path(self):
        # `wake_on_events: []` means "do not re-read an answer already
        # delivered". Nothing was delivered here, so there is no such answer.
        events = [Event("completed")]
        self.assertEqual(
            wake_kinds_for(events, loader({"wake_on_events": []}), adapter=Adapter(False)),
            {"completed"},
        )
        self.assertEqual(
            wake_kinds_for(events, loader({"wake_on_events": []}), adapter=Adapter(True)),
            set(),
        )

    def test_omitting_the_adapter_leaves_the_config_in_charge(self):
        # The notifier always passes it; the default keeps every other caller
        # (and the pre-existing tests above) on the documented config path.
        events = [Event("completed")]
        cfg = loader({"wake_on_events": FAILURE_ONLY})
        self.assertEqual(wake_kinds_for(events, cfg), set())


# =============================================================================
# Recording a suppressed completion
# =============================================================================
#
# The incident these cover: task t_a8f58a2a completed at 19:09:43 and its
# 6,191-character report reached the Slack thread at 19:09:44, but because the
# wake was suppressed nothing entered the creator's transcript. At 19:11:30 the
# front door — whose context still ended at ``subscribed: true`` — told the user
# "You'll see the results post here as soon as the agent completes". The answer
# had been on screen for 106 seconds. Nine and three-quarter minutes later it
# finally called kanban_show.


class _Conversation:
    def __init__(self):
        self.sidecar_notes = []


class _State:
    def __init__(self):
        self.conversation = _Conversation()


class _Entry:
    def __init__(self, session_key):
        self.session_key = session_key


class _Store:
    """Stands in for ``gateway.session.SessionStore``.

    Only the id→key mapping matters here. ``verify_kanban_notifier.py`` drives
    the real store, the real ``SessionEntry`` and the real
    ``lookup_by_session_id`` inside the image.
    """

    def __init__(self, mapping=None, raises=False):
        self.mapping = mapping if mapping is not None else {"sid-1": "key-1"}
        self.raises = raises

    def lookup_by_session_id(self, session_id):
        if self.raises:
            raise RuntimeError("session store unavailable")
        if session_id not in self.mapping:
            return None
        return _Entry(self.mapping[session_id])


class _OldStore(_Store):
    """A session store predating ``lookup_by_session_id``."""

    lookup_by_session_id = None


class _Runner:
    """The four ``GatewayRunner`` members this module touches.

    The three sidecar-note methods are transcribed from ``gateway/run.py``
    (``_set_pending_turn_sidecar_notes`` / ``_consume_pending_turn_sidecar_notes``
    / ``_peek_session_state``) — including the setter's early-out on an empty
    list, which is why ``stage_note`` never hands it one.
    """

    def __init__(self, store=None):
        self.session_store = store if store is not None else _Store()
        self._sessions = {}

    def _peek_session_state(self, session_key):
        return self._sessions.get(session_key)

    def _set_pending_turn_sidecar_notes(self, session_key, notes):
        if not session_key or not notes:
            return
        self._sessions.setdefault(session_key, _State()).conversation.sidecar_notes = list(notes)

    def _consume_pending_turn_sidecar_notes(self, session_key):
        state = self._sessions.get(session_key)
        if state is None:
            return []
        staged = state.conversation.sidecar_notes
        state.conversation.sidecar_notes = []
        return list(staged)


class _Card:
    def __init__(
        self,
        session_id="sid-1",
        title="List configured cron jobs",
        status="done",
        card_id="t_a8f58a2a",
    ):
        self.session_id = session_id
        self.title = title
        self.status = status
        self.id = card_id


def sub_for(task_id="t_a8f58a2a"):
    return {"task_id": task_id, "chat_id": "D0BKGRBM6RH"}


# 2026-08-08 19:09:44 UTC — the moment the report reached the thread.
COMPLETED_AT = 1786216184.0

#: Distinguishes "the caller passed no task" from "the caller passed None",
#: which is a case the notifier has to survive and a default cannot express.
UNSET = object()


class SuppressedKindsTest(unittest.TestCase):
    def test_a_dropped_completion_is_suppressed(self):
        events = [Event("completed"), Event("commented")]
        self.assertEqual(suppressed_kinds(events, set()), {"completed"})

    def test_a_kind_that_still_wakes_is_not_suppressed(self):
        # The wake enters the transcript itself, so it needs no marker.
        events = [Event("completed"), Event("crashed")]
        self.assertEqual(suppressed_kinds(events, {"crashed"}), {"completed"})
        self.assertEqual(suppressed_kinds(events, {"completed", "crashed"}), set())

    def test_upstream_behaviour_suppresses_nothing(self):
        events = [Event("completed"), Event("crashed")]
        woken = wake_kinds_for(events, loader({}))
        self.assertEqual(suppressed_kinds(events, woken), set())

    def test_non_terminal_kinds_are_never_suppressed(self):
        # archived/unblocked are silent by design upstream, not dropped by us.
        for kind in ("commented", "archived", "unblocked", "status"):
            self.assertEqual(suppressed_kinds([Event(kind)], set()), set())

    def test_the_failure_only_config_suppresses_exactly_completion(self):
        events = [Event("completed"), Event("timed_out")]
        woken = wake_kinds_for(events, loader({"wake_on_events": FAILURE_ONLY}))
        self.assertEqual(suppressed_kinds(events, woken), {"completed"})

    def test_junk_inputs_do_not_raise(self):
        self.assertEqual(suppressed_kinds([], None), set())
        self.assertEqual(suppressed_kinds(None, set()), set())
        self.assertEqual(suppressed_kinds([object()], set()), set())


class CompletionNoteTest(unittest.TestCase):
    def note(self, **kw):
        kw.setdefault("kinds", {"completed"})
        kw.setdefault("now", COMPLETED_AT)
        return completion_note(kw.pop("task_id", "t_a8f58a2a"), **kw)

    def test_it_names_the_card_and_the_outcome(self):
        note = self.note(title="List configured cron jobs", status="done")
        self.assertIn("t_a8f58a2a", note)
        self.assertIn("List configured cron jobs", note)
        self.assertIn("completed", note)
        self.assertIn("done", note)

    def test_it_carries_the_time_the_card_finished(self):
        # The gap is the whole complaint: the user waited 9m46s for an answer
        # that was already on screen. A bare "it finished" leaves the agent
        # unable to say how stale its own silence was.
        self.assertIn("2026-08-08 19:09:44 UTC", self.note())

    def test_it_contradicts_the_still_running_guess_explicitly(self):
        # The front door's context ends at `subscribed: true`, so absent a
        # statement to the contrary its default inference is "still running" —
        # which is exactly what it told the user at 19:11:30.
        self.assertIn("NOT still running", self.note())

    def test_it_says_the_result_was_already_delivered(self):
        # Otherwise the marker's own fix is to re-post a 6,191-character report
        # the user is already looking at.
        self.assertIn("already delivered", self.note())

    def test_it_explains_why_the_transcript_has_no_record(self):
        # Without this the agent sees a note contradicted by its own history
        # and has no way to tell which to believe.
        self.assertIn("appears earlier in this transcript", self.note())

    def test_it_points_at_the_tool_that_returns_the_content(self):
        self.assertIn("kanban_show", self.note())

    def test_a_non_default_board_is_named_so_the_lookup_can_succeed(self):
        self.assertIn("(board infra)", self.note(board="infra"))
        self.assertNotIn("board", self.note(board=""))

    def test_every_note_starts_with_the_signature(self):
        for kw in ({}, {"title": ""}, {"status": ""}, {"task_id": ""}):
            self.assertTrue(self.note(**kw).startswith(NOTE_SIGNATURE))

    def test_a_runaway_title_is_clipped_on_a_token_boundary(self):
        # Titles are user-supplied and land in a note that rides the next user
        # message, so an unbounded one is charged to the creator's next turn.
        url = "https://github.com/gke-agentic/adamparco-infra/issues/30"
        note = self.note(title="word " * 400 + url)
        self.assertLess(len(note), 900)
        # Same contract as the status line: a severed link is a dead link, so
        # the URL is dropped whole rather than cut.
        self.assertIn(ELLIPSIS + '")', note)
        self.assertNotIn("github.com", note)

    def test_a_missing_title_is_omitted_rather_than_rendered_empty(self):
        self.assertNotIn('("")', self.note(title=""))
        self.assertNotIn('("None")', self.note(title=None))

    def test_multiple_suppressed_kinds_are_listed_deterministically(self):
        note = self.note(kinds={"timed_out", "completed"})
        self.assertIn("completed, timed_out", note)


class NoteSuppressedCompletionTest(unittest.TestCase):
    def setUp(self):
        _warned_config.clear()
        self.runner = _Runner()

    def record(self, runner=None, events=None, woken=None, task=UNSET, **kw):
        return note_suppressed_completion(
            runner if runner is not None else self.runner,
            events if events is not None else [Event("completed")],
            woken if woken is not None else set(),
            _Card() if task is UNSET else task,
            kw.pop("sub", None) or sub_for(),
            kw.pop("board", ""),
            now=kw.pop("now", COMPLETED_AT),
        )

    # -- the incident ------------------------------------------------------

    def test_the_creators_next_turn_learns_the_card_finished(self):
        self.assertTrue(self.record())
        notes = self.runner._consume_pending_turn_sidecar_notes("key-1")
        self.assertEqual(len(notes), 1)
        self.assertIn("t_a8f58a2a", notes[0])
        self.assertIn("NOT still running", notes[0])

    def test_the_marker_is_one_shot(self):
        # A note replayed on every later turn would be worse than no note: the
        # front door would keep announcing a card it already reported.
        self.record()
        self.assertTrue(self.runner._consume_pending_turn_sidecar_notes("key-1"))
        self.assertEqual(self.runner._consume_pending_turn_sidecar_notes("key-1"), [])

    def test_the_marker_lands_on_the_key_not_the_session_id(self):
        # task.session_id is the persisted id; per-turn state is keyed by the
        # session key. Writing to the id would be a silent no-op forever.
        self.record()
        self.assertIn("key-1", self.runner._sessions)
        self.assertNotIn("sid-1", self.runner._sessions)

    def test_nothing_is_recorded_when_the_wake_still_fires(self):
        self.assertFalse(self.record(woken={"completed"}))
        self.assertEqual(self.runner._sessions, {})

    def test_an_unconfigured_gateway_records_nothing_at_all(self):
        # kanban.wake_on_events unset ⇒ upstream wake set ⇒ nothing suppressed.
        events = [Event("completed"), Event("crashed")]
        self.assertFalse(self.record(events=events, woken=wake_kinds_for(events, loader({}))))
        self.assertEqual(self.runner._sessions, {})

    def test_a_failure_only_gateway_records_the_completion(self):
        events = [Event("completed")]
        woken = wake_kinds_for(events, loader({"wake_on_events": FAILURE_ONLY}))
        self.assertTrue(self.record(events=events, woken=woken))

    # -- independence from the subscription --------------------------------

    def test_the_marker_survives_the_subscription_being_deleted(self):
        # On a terminal event the notifier calls _kanban_unsub moments later
        # and the row is gone. The note is gateway session state and never
        # referred to the row, so deleting it changes nothing.
        sub = sub_for()
        self.record(sub=sub)
        sub.clear()  # what _kanban_unsub amounts to, from the note's point of view
        notes = self.runner._consume_pending_turn_sidecar_notes("key-1")
        self.assertIn("t_a8f58a2a", notes[0])

    # -- sharing the list with upstream ------------------------------------

    def test_upstreams_own_notes_are_not_clobbered(self):
        # _set_pending_turn_sidecar_notes assigns the whole list. The auto-reset
        # notice tells the agent its history is gone; losing it to a kanban
        # marker would be a strictly worse bug than the one being fixed.
        reset = "[System note: The user's previous session expired due to inactivity...]"
        self.runner._set_pending_turn_sidecar_notes("key-1", [reset])
        self.record()
        notes = self.runner._consume_pending_turn_sidecar_notes("key-1")
        self.assertIn(reset, notes)
        self.assertEqual(len(notes), 2)

    def test_the_same_card_is_never_recorded_twice(self):
        self.assertTrue(self.record())
        self.assertFalse(self.record())
        self.assertEqual(len(self.runner._consume_pending_turn_sidecar_notes("key-1")), 1)

    def test_a_card_id_that_prefixes_another_is_still_recorded(self):
        # Dedupe matches on the id plus a trailing space. Without the space,
        # t_a8 finishing first would silence t_a8f58a2a.
        self.assertTrue(self.record(sub=sub_for("t_a8")))
        self.assertTrue(self.record(sub=sub_for("t_a8f58a2a")))
        notes = self.runner._consume_pending_turn_sidecar_notes("key-1")
        self.assertEqual(len(notes), 2)

    def test_our_notes_are_capped_and_upstreams_are_not_evicted(self):
        reset = "[System note: The user's previous session expired...]"
        self.runner._set_pending_turn_sidecar_notes("key-1", [reset])
        for i in range(MAX_NOTES + 4):
            self.record(sub=sub_for(f"t_{i}"))
        notes = self.runner._consume_pending_turn_sidecar_notes("key-1")
        self.assertIn(reset, notes)
        self.assertEqual(len(notes), MAX_NOTES + 1)
        # The most recent completions are the ones kept.
        self.assertTrue(any(f"t_{MAX_NOTES + 3} " in n for n in notes))
        self.assertFalse(any("t_0 " in n for n in notes))

    # -- fail-soft ---------------------------------------------------------

    def test_a_card_with_no_creator_session_records_nothing(self):
        # Cron and CLI cards have no gateway session to tell.
        self.assertFalse(self.record(task=_Card(session_id=None)))
        self.assertEqual(self.runner._sessions, {})

    def test_a_rotated_or_unknown_session_records_nothing(self):
        self.assertFalse(self.record(task=_Card(session_id="sid-gone")))
        self.assertEqual(self.runner._sessions, {})

    def test_a_raising_session_store_does_not_break_delivery(self):
        runner = _Runner(_Store(raises=True))
        with self.assertLogs("gateway.run", level=logging.WARNING):
            self.assertFalse(self.record(runner=runner))

    def test_a_store_without_the_lookup_says_so_once(self):
        runner = _Runner(_OldStore())
        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            for _ in range(20):
                self.record(runner=runner)
        # The notifier polls every 5s; a per-delivery warning would be the
        # loudest line in the log.
        self.assertEqual(len(captured.output), 1)
        self.assertIn("lookup_by_session_id", "\n".join(captured.output))

    def test_a_runner_without_the_sidecar_channel_says_so_once(self):
        class _Bare(_Runner):
            _set_pending_turn_sidecar_notes = None

        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            for _ in range(20):
                self.record(runner=_Bare())
        self.assertEqual(len(captured.output), 1)
        self.assertIn("_set_pending_turn_sidecar_notes", "\n".join(captured.output))

    def test_a_missing_task_row_does_not_raise(self):
        self.assertFalse(self.record(task=None))

    def test_an_exploding_task_row_does_not_raise(self):
        class Exploding:
            @property
            def session_id(self):
                raise RuntimeError("boom")

        with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
            self.assertFalse(self.record(task=Exploding()))
        # The card id has to survive into the warning or the line is unactionable.
        self.assertIn("t_a8f58a2a", "\n".join(captured.output))

    def test_a_successful_record_is_logged_at_info(self):
        # On a narrowed gateway this is the only evidence the creator was told
        # anything; at debug it would be invisible in production.
        with self.assertLogs("gateway.run", level=logging.INFO) as captured:
            self.record()
        joined = "\n".join(captured.output)
        self.assertIn("t_a8f58a2a", joined)
        self.assertIn("key-1", joined)


class CreatorSessionKeyTest(unittest.TestCase):
    def setUp(self):
        _warned_config.clear()

    def test_it_resolves_the_key_behind_the_persisted_id(self):
        runner = _Runner(_Store({"sid-1": "agent:main:slack:dm:T0:D0:1786216044.637229"}))
        self.assertEqual(
            creator_session_key(runner, _Card()),
            "agent:main:slack:dm:T0:D0:1786216044.637229",
        )

    def test_an_entry_without_a_key_resolves_to_nothing(self):
        runner = _Runner(_Store({"sid-1": ""}))
        self.assertEqual(creator_session_key(runner, _Card()), "")

    def test_a_blank_session_id_is_debug_not_a_warning(self):
        # Cron and CLI cards are the common case, not a fault; warning here
        # would put a line in the log every five seconds on a busy board.
        with self.assertLogs("gateway.run", level=logging.DEBUG) as captured:
            self.assertEqual(creator_session_key(_Runner(), _Card(session_id="")), "")
        self.assertEqual([r.levelno for r in captured.records], [logging.DEBUG])

    def test_an_unresolvable_session_id_is_debug_not_a_warning(self):
        with self.assertLogs("gateway.run", level=logging.DEBUG) as captured:
            self.assertEqual(creator_session_key(_Runner(), _Card(session_id="sid-gone")), "")
        self.assertEqual([r.levelno for r in captured.records], [logging.DEBUG])


# =============================================================================
# Storing the report for the reply
# =============================================================================

#: What a Cluster Agent completes an event-triage card with — the shape
#: `session_kv_server._triage_task_body` asks for, abridged.
TRIAGE_REPORT = """\
## What's wrong

The `checkout` deployment cannot schedule: every replica is Pending.

## Why

- The pod requests 8Gi and every node in `default-pool` has 4Gi allocatable
  (`kubectl describe node` → `Allocatable: memory: 3910Mi`).

## What to do

- **Option A (Right-size the request):** drop `resources.requests.memory` to 2Gi.
- **Option B (Add a larger node pool):** create an `e2-standard-8` pool.
- ✅ **Recommended: Option A** — no new capacity to pay for or drain later.
"""

#: The other common completion: a card that did its job and has nothing to
#: apply. Storing this would shadow a real report in the same thread for the
#: whole of CLEANUP_TTL_DAYS, because POST /v1/incidents keeps the first row.
STATUS_ONLY_RESULT = "Checked all 14 clusters. No configuration drift found."


def sub_with_thread(chat_id="D0BKGRBM6RH", thread_id="1786216044.637229"):
    """A subscription row after kanban_event_routing substituted a chat route."""
    return {"task_id": "t_a8f58a2a", "chat_id": chat_id, "thread_id": thread_id}


@contextlib.contextmanager
def captured_post(fail=None):
    """Intercept the loopback POST and collect the urllib Requests it made."""
    posted = []

    def _urlopen(request, timeout=None):
        posted.append(request)
        if fail is not None:
            raise fail
        return contextlib.nullcontext()

    with mock.patch("kanban_notifier.urllib.request.urlopen", _urlopen):
        yield posted


def posted_body(request):
    return json.loads(request.data.decode())


class ActionableReportTest(unittest.TestCase):
    """The gate on which completions get an `incidents` row."""

    def test_a_triage_report_is_actionable(self):
        self.assertTrue(actionable_report(TRIAGE_REPORT))

    def test_a_status_line_is_not(self):
        # The failure this gate exists for. INSERT OR IGNORE keeps the first
        # report per thread, so a status line stored here is not a wasted row —
        # it is the row a later real report cannot replace.
        self.assertFalse(actionable_report(STATUS_ONLY_RESULT))

    def test_a_single_option_report_is_actionable(self):
        # The shape with no letter in it at all. One sound fix is not "Option
        # A" — the template drops the letter and the Recommended line with it,
        # leaving the call to action as the only thing under the heading. The
        # reply it invites is a bare "apply", which is exactly the reply that
        # needs the row: nothing in the words themselves says which report.
        self.assertTrue(
            actionable_report(
                "## What to do\n\n"
                "- **Proposed fix (Bump the limit):** raise it to 2Gi.\n"
                "- **To authorize:** reply **'apply'** to open a GitOps Pull "
                "Request with this fix.\n"
            )
        )

    def test_the_heading_alone_is_not_enough(self):
        self.assertFalse(actionable_report("## What to do\n\n- Restart the pod.\n"))

    def test_a_call_to_action_above_the_heading_does_not_count(self):
        # The unlettered half of the "under it is literal" rule below. A card
        # quoting an older report's call to action in its prose has nothing of
        # its own to apply, and would take the thread's one INSERT OR IGNORE
        # slot from the report that has.
        self.assertFalse(
            actionable_report(
                "## Why\n\nThe **To authorize:** bullet went unanswered.\n\n"
                "## What to do\n\n- Escalate to the service owner.\n"
            )
        )

    def test_an_option_named_above_the_heading_does_not_count(self):
        # A report whose "What to do" holds only unlettered bullets, but which
        # quotes an earlier report's Option A further up. Searching the whole
        # body would take the thread's one INSERT OR IGNORE slot on a report
        # with nothing to apply, and hold it against the one that has.
        self.assertFalse(
            actionable_report(
                "## Why\n\nThe fix applied as Option A last week has regressed.\n\n"
                "## What to do\n\n- Escalate to the service owner.\n"
            )
        )

    def test_the_word_option_in_prose_is_not_a_label(self):
        # Lowercase, and no heading: an ordinary sentence, not a labelled bullet.
        self.assertFalse(
            actionable_report("There is no good option here; escalate to the owner.")
        )

    def test_authorize_in_prose_is_not_a_call_to_action(self):
        # The counterpart for the unlettered shape. "to authorize" turns up in
        # ordinary remediation prose, and a card that merely mentions it offers
        # a reply nothing to act on — but would still take the thread's one
        # INSERT OR IGNORE slot from the triage report that follows. The colon
        # is what separates the template's bullet label from the preposition.
        self.assertFalse(
            actionable_report(
                "## What to do\n\n"
                "- Escalate to the service owner to authorize the quota increase.\n"
            )
        )

    def test_the_call_to_action_counts_however_it_is_emphasised(self):
        # The template writes **To authorize:** with the colon inside the
        # emphasis, but an agent reproducing a **Label:** bullet moves the
        # marker as readily as not, and italic and __-bold say the same thing.
        # Matching only the template's spelling fails these silently: the
        # single-option shape has no lettered option to fall back on, so the
        # report is delivered, no row is written, and the "apply" it invites
        # arrives with nothing attached.
        for label in (
            "**To authorize:**",
            "**To authorize**:",
            "*To authorize*:",
            "__To authorize__:",
            "To authorize:",
        ):
            with self.subTest(label=label):
                self.assertTrue(
                    actionable_report(
                        "## What to do\n\n"
                        "- **Proposed fix (Bump the limit):** raise it to 2Gi.\n"
                        "- %s reply **'apply'** to open a GitOps Pull Request "
                        "with this fix.\n" % label
                    )
                )

    def test_an_empty_or_missing_result_is_not_actionable(self):
        for result in (None, "", "   ", 0):
            self.assertFalse(actionable_report(result), result)


class StoreIncidentReportTest(unittest.TestCase):
    def test_a_completed_triage_report_is_stored_against_its_thread(self):
        with captured_post() as posted:
            self.assertTrue(
                store_incident_report(
                    Event("completed"),_Task(TRIAGE_REPORT), sub_with_thread()
                )
            )
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].full_url, "http://127.0.0.1:8699/v1/incidents")
        self.assertEqual(posted[0].get_method(), "POST")
        body = posted_body(posted[0])
        self.assertEqual(body["chat_id"], "D0BKGRBM6RH")
        self.assertEqual(body["thread_id"], "1786216044.637229")
        # The whole report, not the status line: "apply Option B" has to resolve
        # to the option, and the option is three quarters of the way down.
        self.assertIn("Option B (Add a larger node pool)", body["report"])

    def test_the_api_key_is_sent(self):
        # Every /v1/incidents route is authenticated. Without the header the
        # POST is a 401 that this function would swallow as a warning, and the
        # row would never exist — the exact failure being fixed, one layer over.
        with mock.patch.dict(os.environ, {"SESSION_KV_API_KEY": "s3cret"}):
            with captured_post() as posted:
                store_incident_report(
                    Event("completed"),_Task(TRIAGE_REPORT), sub_with_thread()
                )
        self.assertEqual(posted[0].get_header("Authorization"), "Bearer s3cret")

    def test_a_status_only_card_stores_nothing(self):
        with captured_post() as posted:
            self.assertFalse(
                store_incident_report(
                    Event("completed"),_Task(STATUS_ONLY_RESULT), sub_with_thread()
                )
            )
        self.assertEqual(posted, [])

    def test_a_status_only_card_does_not_warn(self):
        # Most cards look like this. A warning here would put a line in the log
        # for every ordinary completion on every board.
        with captured_post():
            with self.assertLogs("gateway.run", level=logging.DEBUG) as captured:
                store_incident_report(
                    Event("completed"),_Task(STATUS_ONLY_RESULT), sub_with_thread()
                )
        self.assertEqual([r.levelno for r in captured.records], [logging.DEBUG])

    def test_a_non_terminal_or_failed_card_stores_nothing(self):
        for kind in ("commented", "crashed", "gave_up", "timed_out", "blocked"):
            with captured_post() as posted:
                self.assertFalse(
                    store_incident_report(
                        Event(kind), _Task(TRIAGE_REPORT), sub_with_thread()
                    ),
                    kind,
                )
            self.assertEqual(posted, [], kind)

    def test_the_commented_event_of_a_completed_delivery_stores_nothing(self):
        # The call site runs once per event, inside `for ev in d["events"]:`.
        # A delivery bundling [commented, completed] reaches this function
        # twice; only the second one has sent the report. Deciding from the
        # delivery's whole kind set would store the row on the first pass,
        # before the report the row claims the reader has was posted.
        with captured_post() as posted:
            self.assertFalse(
                store_incident_report(
                    Event("commented"), _Task(TRIAGE_REPORT), sub_with_thread()
                )
            )
        self.assertEqual(posted, [])

    def test_a_wake_only_subscription_stores_nothing(self):
        # delivery_mode="wake" wakes the agent and posts nothing in the thread.
        # There is no delivered report to reply to, and a row written anyway
        # would prepend a report the user never saw to their next message for
        # the whole of the table's retention window.
        with captured_post() as posted:
            with self.assertLogs("gateway.run", level=logging.DEBUG) as captured:
                self.assertFalse(
                    store_incident_report(
                        Event("completed"),
                        _Task(TRIAGE_REPORT),
                        sub_with_thread(),
                        posted=False,
                    )
                )
        self.assertEqual(posted, [])
        self.assertEqual([r.levelno for r in captured.records], [logging.DEBUG])

    def test_an_unthreaded_delivery_warns(self):
        # The by-thread lookup is keyed on both halves, so a report delivered to
        # the channel body can never be found again. Failing open is right;
        # failing open silently is the bug this whole change is about.
        with captured_post() as posted:
            with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
                self.assertFalse(
                    store_incident_report(
                        Event("completed"),
                        _Task(TRIAGE_REPORT),
                        sub_with_thread(thread_id=""),
                    )
                )
        self.assertEqual(posted, [])
        self.assertIn("t_a8f58a2a", captured.output[0])

    def test_a_failed_post_warns_and_does_not_raise(self):
        # An exception escaping here reaches the notifier tick, which rewinds
        # the cursor and re-posts a report the user has already read.
        with captured_post(fail=OSError("connection refused")) as posted:
            with self.assertLogs("gateway.run", level=logging.WARNING) as captured:
                self.assertFalse(
                    store_incident_report(
                        Event("completed"),_Task(TRIAGE_REPORT), sub_with_thread()
                    )
                )
        self.assertEqual(len(posted), 1)
        self.assertIn("t_a8f58a2a", captured.output[0])
        self.assertIn("1786216044.637229", captured.output[0])

    def test_a_hostile_task_object_does_not_raise(self):
        class Exploding:
            @property
            def result(self):
                raise RuntimeError("no result for you")

        with captured_post() as posted:
            with self.assertLogs("gateway.run", level=logging.WARNING):
                self.assertFalse(
                    store_incident_report(
                        Event("completed"),Exploding(), sub_with_thread()
                    )
                )
        self.assertEqual(posted, [])

    def test_a_missing_task_or_subscription_does_not_raise(self):
        with captured_post() as posted:
            self.assertFalse(store_incident_report(Event("completed"), None, None))
        self.assertEqual(posted, [])

    def test_an_oversized_report_is_stored_at_the_delivered_length(self):
        # Storing more than the reader was shown would let the agent answer
        # about options that never reached the thread.
        body = TRIAGE_REPORT + "\n" + ("filler line\n" * 5000)
        self.assertGreater(len(body), RESULT_LIMIT)
        with captured_post() as posted:
            store_incident_report(Event("completed"), _Task(body), sub_with_thread())
        self.assertLessEqual(len(posted_body(posted[0])["report"]), RESULT_LIMIT)

    def test_a_stored_report_says_so_at_info(self):
        # The only positive evidence in the log that turn ② is reachable.
        with captured_post():
            with self.assertLogs("gateway.run", level=logging.INFO) as captured:
                store_incident_report(
                    Event("completed"),_Task(TRIAGE_REPORT), sub_with_thread()
                )
        self.assertEqual([r.levelno for r in captured.records], [logging.INFO])
        self.assertIn("t_a8f58a2a", captured.output[0])


# =============================================================================
# The applier
# =============================================================================

# The notifier loop reduced to the lines the patch rewrites, kept at its real
# nesting depth because both anchors are indentation-sensitive. The `msg = (`
# block below carries no anchor any more — it is here because the hook has to
# land between the clip and it, and that ordering is the whole contract.
UPSTREAM_WATCHERS = '''\
class GatewayKanbanWatchers:
    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        while self._running:
            try:
                for d in deliveries:
                    mode = sub.get("delivery_mode") or "notify"
                    wake_agent = mode in ("notify+wake", "wake")
                    send_passive = mode != "wake"
                    for ev in d["events"]:
                        kind = ev.kind
                        if kind == "completed":
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                lines = payload_summary.strip().splitlines()
                                h = lines[0][:200] if lines else payload_summary[:200]
                                handoff = f"\\n{h}"
                                wake_handoff = h
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\\n{r}"
                                wake_handoff = r
                            msg = (
                                f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            msg = "blocked"
                        await adapter.send(sub["chat_id"], msg)
                        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
                        _wake_kinds = (
                            {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}
                            if wake_agent
                            else set()
                        )
                        if "completed" in _wake_kinds:
                            pass
            except Exception:
                pass
'''


def patch_tree(source):
    """Write ``source`` as gateway/kanban_watchers.py under a temp root and patch it."""
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


class ApplyTest(unittest.TestCase):
    def test_both_anchors_match_upstream_exactly_once(self):
        self.assertEqual(UPSTREAM_WATCHERS.count(HANDOFF_ANCHOR), 1)
        self.assertEqual(UPSTREAM_WATCHERS.count(WAKE_ANCHOR), 1)

    def test_the_message_block_is_no_longer_an_anchor(self):
        # The reduction the merge buys. The old delivery applier had to match
        # the `msg = (` f-strings — three lines of nested quotes and unicode —
        # purely to find an insertion point after clip lines another patch
        # owned. Owning both, this applier appends the hook to its own
        # replacement instead, so upstream can reword that message freely.
        self.assertNotIn("msg = (", HANDOFF_ANCHOR + WAKE_ANCHOR)

    def test_both_of_upstreams_hard_slices_are_replaced(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertIn("h = _clip_handoff(payload_summary)", patched)
        self.assertIn("r = _clip_handoff(task.result)", patched)
        self.assertNotIn("lines[0][:200]", patched)
        self.assertNotIn("lines[0][:160]", patched)

    def test_the_hardcoded_tuple_is_replaced_by_the_helper(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        # Both keyword arguments are part of the assertion: the notifier must
        # hand the helper the adapter and the delivery mode, or neither no-send
        # carve-out above ever engages.
        self.assertIn(
            'd["events"], adapter=adapter, passive_delivered=send_passive', patched
        )
        self.assertNotIn('_WAKE_KINDS = ("completed"', patched)
        self.assertNotIn("in _WAKE_KINDS}", patched)
        # Upstream's own per-subscription gate survives the replacement. Losing
        # it would wake a mode="notify" subscriber this patch never had an
        # opinion about.
        self.assertIn("if wake_agent", patched)
        self.assertIn("else set()", patched)

    def test_the_hook_lands_after_the_clip_and_before_the_message(self):
        # Ordering is the whole contract: the hook has to see the handoff the
        # clip produced in order to decide the clip was redundant, and the
        # message has to be built from what the hook returned.
        patched = patch_tree(UPSTREAM_WATCHERS)
        clip = patched.rindex("r = _clip_handoff(task.result)")
        hook = patched.index("handoff = _kanban_handoff_with_result(handoff, task)")
        message = patched.index("msg = (")
        self.assertTrue(clip < hook < message)

    def test_the_hook_replaces_the_handoff_rather_than_appending_to_it(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertNotIn("handoff +=", patched)

    def test_one_import_trailer_carries_every_name(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertIn("from gateway.kanban_notifier import", patched)
        for name in (
            "clip_handoff as _clip_handoff",
            "handoff_with_result as _kanban_handoff_with_result",
            "note_suppressed_completion as _kanban_note_suppressed",
            "store_incident_report as _kanban_store_incident",
            "wake_kinds_for as _wake_kinds_for",
        ):
            self.assertIn(name, patched)
        # Three trailers became one; a second would mean a duplicated build step.
        self.assertEqual(patched.count("from gateway.kanban_notifier import"), 1)

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(UPSTREAM_WATCHERS))

    def test_a_drifted_handoff_anchor_fails_loudly(self):
        drifted = UPSTREAM_WATCHERS.replace("lines[0][:200]", "lines[0][:220]")
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))
        self.assertIn("completion handoff", str(ctx.exception))

    def test_a_drifted_wake_anchor_fails_loudly(self):
        # Names the failing anchor: with two edits in one applier, "found 0" on
        # its own would not say which half of the notifier moved.
        drifted = UPSTREAM_WATCHERS.replace('"blocked")', '"blocked", "abandoned")')
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))
        self.assertIn("wake set", str(ctx.exception))

    def test_a_drifted_second_anchor_leaves_the_file_untouched(self):
        # The applier edits a string and writes once at the end, so a failure
        # on the second anchor must not leave the first edit on disk.
        drifted = UPSTREAM_WATCHERS.replace('"blocked")', '"blocked", "abandoned")')
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(drifted)
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), drifted)

    def test_applying_twice_fails_rather_than_silently_no_opping(self):
        # Both anchors are destroyed by their own replacement, so a re-run would
        # fail on "found 0" anyway — but that message blames upstream drift for
        # what is really a duplicated build step, and the old delivery applier
        # had an anchor that survived patching and did silently stack.
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM_WATCHERS)
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("already patched", str(ctx.exception))
        patched = target.read_text()
        self.assertEqual(
            patched.count("handoff = _kanban_handoff_with_result(handoff, task)"), 1
        )
        self.assertEqual(patched.count("from gateway.kanban_notifier import"), 1)

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))


# The three edits this applier replaces, as they were: the Dockerfile's inline
# `kanban_handoff_clip` rewrite, `apply_kanban_wake_kinds.py`, and
# `apply_kanban_result_delivery.py`. Kept here rather than deleted with them so
# the equivalence claim stays checkable after the originals are gone.
LEGACY_CLIP = (
    (
        "h = lines[0][:200] if lines else payload_summary[:200]",
        "h = _clip_handoff(payload_summary)",
    ),
    (
        "r = lines[0][:160] if lines else task.result[:160]",
        "r = _clip_handoff(task.result)",
    ),
)
LEGACY_WAKE = (
    '                        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")\n'
    '                        _wake_kinds = (\n'
    '                            {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}\n'
    '                            if wake_agent\n'
    '                            else set()\n'
    '                        )\n',
    '                        _wake_kinds = (\n'
    '                            _wake_kinds_for(d["events"], adapter=adapter)\n'
    '                            if wake_agent\n'
    '                            else set()\n'
    '                        )\n',
)
LEGACY_DELIVERY = (
    '                            msg = (\n',
    "                            handoff = _kanban_handoff_with_result(handoff, task)\n"
    "                            msg = (\n",
)


def legacy_pipeline(source):
    """Replay the three superseded edits in the order the Dockerfile ran them."""
    for old, new in LEGACY_CLIP:
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    for old, new in (LEGACY_WAKE, LEGACY_DELIVERY):
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    return source


#: The wake call as the merged applier emits it, and as the three superseded
#: appliers emitted it. They differ by ``passive_delivered=send_passive``, which
#: is a behaviour change and not a refactor: v2026.8.13's ``delivery_mode="wake"``
#: skips the text ping on a push adapter, so the narrowing had to learn a second
#: way for "the answer is already in the thread" to be false. Held as literals
#: rather than read off the applier so that changing the applier changes this
#: file too, where a reviewer will see it.
WAKE_CALL_MERGED = (
    "                            _wake_kinds_for(\n"
    '                                d["events"], adapter=adapter, passive_delivered=send_passive\n'
    "                            )\n"
)
WAKE_CALL_LEGACY = '                            _wake_kinds_for(d["events"], adapter=adapter)\n'


def strip_patch_furniture(text, drop_added_calls=True, normalise_wake_call=True):
    """Reduce patched source to the part the legacy pipeline can be compared to.

    Five things are dropped or rewritten, and only five:

    * the import trailer — three trailers became one;
    * the ``see <module>`` comments — they now name one module;
    * the marker call and the incident-store call, when ``drop_added_calls`` is
      set. These are the pieces of emitted code with no legacy counterpart,
      because they are new behaviours (sections 4 and 5 of
      ``kanban_notifier.py``);
    * the wake call's ``passive_delivered=`` argument, when
      ``normalise_wake_call`` is set — the third new behaviour, and the reason
      the merged call wraps where the legacy one did not.

    Subtracting those is what lets :class:`LegacyEquivalenceTest` keep making
    its original claim about everything else; that the subtractions are the
    *whole* difference is asserted separately for each, so nothing can hide
    behind any of them.
    """
    marker = "\n\n# kube-agents patch: see gateway/"
    body = text[: text.index(marker)] if marker in text else text
    if drop_added_calls:
        body = body.replace(INCIDENT_CALL, "").replace(MARKER_CALL, "")
    if normalise_wake_call:
        body = body.replace(WAKE_CALL_MERGED, WAKE_CALL_LEGACY)
    return "\n".join(
        line for line in body.splitlines()
        if "# kube-agents patch: see gateway/" not in line
    )


class LegacyEquivalenceTest(unittest.TestCase):
    """The merge is a refactor, and this is the proof.

    Two anchors in one applier produce the same patched notifier as the four
    anchors across three appliers did. Anything else — a dropped ``lines = …``,
    a reordered hook, a changed clip call — would be a behaviour change wearing
    a refactor's clothes, and the duplicate-delivery bug this code path already
    shipped once is exactly the kind of thing that hides in "while I was in
    there".

    Three later additions are deliberate exceptions, and they are the only
    ones: the completion marker, the incident-store call, and the wake call's
    ``passive_delivered=`` argument. Each is normalised away before the
    comparison and pinned by its own test —
    :meth:`test_the_added_calls_are_the_only_departure_from_legacy`,
    :meth:`test_the_incident_call_is_emitted_exactly_once` and
    :meth:`test_the_wake_call_carries_the_delivery_mode_argument` — so the
    equivalence claim narrowed by exactly three reviewable blocks rather than
    quietly weakening.
    """

    def test_the_merged_applier_reproduces_the_legacy_output(self):
        self.assertEqual(
            strip_patch_furniture(legacy_pipeline(UPSTREAM_WATCHERS)),
            strip_patch_furniture(patch_tree(UPSTREAM_WATCHERS)),
        )

    def test_the_added_calls_are_the_only_departure_from_legacy(self):
        # Without this, strip_patch_furniture's replace() would be a hole any
        # future edit could be slipped through. Compare the two outputs with
        # nothing subtracted and require every added line to belong to one of
        # the two added calls.
        legacy = strip_patch_furniture(
            legacy_pipeline(UPSTREAM_WATCHERS), drop_added_calls=False
        ).splitlines()
        merged = strip_patch_furniture(
            patch_tree(UPSTREAM_WATCHERS), drop_added_calls=False
        ).splitlines()
        added = [line for line in merged if line not in legacy]
        # The marker call's closing `)` is not among them: since v2026.8.13 the
        # wake-set assignment legacy also produces is a parenthesized
        # conditional, so a bare `)` at that indent already appears in both.
        # Membership, not identity, is what this comparison can see.
        self.assertEqual(added, [
            line
            for line in (INCIDENT_CALL + MARKER_CALL).splitlines()
            if line not in legacy
        ])
        self.assertIn("wake_configured=wake_agent,", "\n".join(added))
        self.assertIn(INCIDENT_CALL.strip(), "\n".join(added))
        # ...and nothing was removed, either.
        self.assertEqual([line for line in legacy if line not in merged], [])

    def test_the_marker_call_is_emitted_exactly_once(self):
        # A second copy would announce the same card twice on one turn, and is
        # what a re-applied patch used to produce before SENTINELS grew a guard
        # for this name.
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertEqual(patched.count("_kanban_note_suppressed("), 1)
        self.assertEqual(patched.count("as _kanban_note_suppressed,"), 1)
        self.assertIn(MARKER_CALL, patched)

    def test_the_incident_call_is_emitted_exactly_once(self):
        # A second copy would POST the same row twice per delivery. Harmless
        # against INSERT OR IGNORE, but it would double the loopback traffic on
        # the notifier's poll loop and mask a duplicated build step.
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertEqual(patched.count("_kanban_store_incident("), 1)
        self.assertEqual(patched.count("as _kanban_store_incident,"), 1)
        self.assertIn(INCIDENT_CALL, patched)

    def test_the_incident_call_is_passed_this_event_and_not_the_delivery(self):
        # The anchor is 24 spaces in, which is inside `for ev in d["events"]:`
        # as well as `for d in deliveries:` — so this call runs once per event.
        # Handed `d["events"]` it would fire on a delivery's `commented` event
        # too, writing the row before the `completed` iteration sends the
        # report the row claims the reader has. `posted=send_passive` is the
        # other half: with delivery_mode="wake" nothing is posted at all.
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertIn("_kanban_store_incident(ev, task, sub", patched)
        self.assertNotIn('_kanban_store_incident(d["events"]', patched)
        self.assertIn("posted=send_passive", INCIDENT_CALL)

    def test_the_incident_call_runs_after_the_report_was_sent(self):
        # The row asserts that the reader HAS this report, so it must not be
        # written on a path that has not sent it. Upstream's `adapter.send` is
        # the last thing before this anchor.
        patched = patch_tree(UPSTREAM_WATCHERS)
        send = patched.index("await adapter.send(")
        store = patched.index(INCIDENT_CALL.strip())
        self.assertLess(send, store)

    def test_the_incident_call_comes_before_the_marker_call(self):
        # Ordering is not load-bearing — neither can raise — but it is asserted
        # so a reordering is a deliberate edit rather than a rebase artifact.
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertLess(
            patched.index(INCIDENT_CALL.strip()),
            patched.index("_kanban_note_suppressed(\n"),
        )

    def test_the_marker_call_reads_the_wake_set_it_is_reporting_on(self):
        # It subtracts the wake set from what upstream would have woken for, so
        # it cannot run before `_wake_kinds` exists.
        patched = patch_tree(UPSTREAM_WATCHERS)
        wake = patched.index(
            'd["events"], adapter=adapter, passive_delivered=send_passive'
        )
        note = patched.index("_kanban_note_suppressed(\n")
        self.assertLess(wake, note)

    def test_the_wake_call_carries_the_delivery_mode_argument(self):
        # The second departure from legacy, pinned the way the marker call is:
        # strip_patch_furniture rewrites the merged call back to the legacy
        # one-liner, and without this that rewrite would be a hole a dropped
        # argument could vanish through — leaving a build that narrows the wake
        # for delivery_mode="wake" subscribers, whose wake IS the delivery.
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertIn(WAKE_CALL_MERGED, patched)
        self.assertNotIn(WAKE_CALL_LEGACY, patched)
        # And it binds upstream's own name for "this mode gets a text ping",
        # not a literal that would silently stop tracking the mode.
        self.assertIn('send_passive = mode != "wake"', patched)

    def test_upstreams_own_lines_are_left_alone(self):
        # Both `lines = …` assignments are dead once the clip lands, but they
        # are upstream's dead code, not ours. Removing them would put an edit
        # in the patch that no anchor and no test was asking for.
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertIn("lines = payload_summary.strip().splitlines()", patched)
        self.assertIn("lines = task.result.strip().splitlines()", patched)


class VerifierSendAnchorTest(unittest.TestCase):
    """The one literal in ``verify_kanban_notifier.py`` another patch owns.

    The verifier asserts the incident row is written after the report was sent
    by comparing source offsets, and the send it measures against is not
    upstream's ``await adapter.send(`` — ``apply_kanban_progress_lines.py``
    rewrites that line earlier in the same build. Nothing else couples the two
    files, and a mismatch is silent in the worst way: ``str.find`` returns -1,
    the offset comparison fails, and the build reports "the row is written
    before the report was sent" about code whose ordering is fine. That is the
    build this test was written after.
    """

    ANCHOR_PATTERN = r'_send_at = NOTIFIER_SOURCE\.find\("([^"]+)"\)'

    def _anchor(self):
        match = re.search(self.ANCHOR_PATTERN, VERIFIER_SOURCE)
        self.assertIsNotNone(
            match, "verify_kanban_notifier.py no longer derives _send_at this way"
        )
        return match.group(1)

    def test_the_anchor_is_text_the_progress_lines_patch_emits(self):
        self.assertIn(self._anchor(), SEND_PATCHED)

    def test_the_anchor_is_not_the_text_that_patch_replaced(self):
        # Guards the specific regression rather than its shape: reverting to
        # upstream's spelling passes every other test in this file, because no
        # other test in this file reads the progress-lines patch at all.
        self.assertNotIn(self._anchor(), SEND_ANCHOR)

    def test_the_verifier_fails_loudly_when_the_anchor_moves(self):
        self.assertIn(
            "the send this ordering is measured against is still there",
            VERIFIER_SOURCE,
            "without a presence check, a moved anchor is reported as a "
            "wrong-order bug that does not exist",
        )


if __name__ == "__main__":
    unittest.main()
