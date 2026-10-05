"""Host tests for the KAGE_SLACK_UX failure-reply module. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_failure.py
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import apply_slack_ux_failure as applier
import slack_presenter
import slack_ux_failure as runtime
import slack_ux_moments
import verify_slack_ux_failure as verifier

#: Mock 06's reply, verbatim.
REPLY = (
    "I couldn't find seeded-z. The fleet has seeded-a, -b and -c. checkout-gateway runs on seeded-a. Check it there?"
)
SUB = {"task_id": "t_f1", "platform": "slack", "chat_id": "C0KAGE", "thread_id": "1700000000.000100"}
WAKE = "Task t_f1 gave up."
FLAG_ENV = "KAGE_SLACK_UX"
USER_MESSAGE_ID = "1700000001.000200"


def _event(internal=True, platform="slack", chat_id="C0KAGE", thread_id="1700000000.000100"):
    source = SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id=chat_id, thread_id=thread_id)
    message_id = None if internal else USER_MESSAGE_ID
    return SimpleNamespace(internal=internal, source=source, message_id=message_id, text="", timestamp=datetime.now())


def _later(**kwargs):
    event = _event(**kwargs)
    event.timestamp += timedelta(seconds=1)
    return event


def _earlier(**kwargs):
    event = _event(**kwargs)
    event.timestamp -= timedelta(seconds=1)
    return event


def _turn(when=0, **kwargs):
    """An event whose turn has started, ``when`` seconds from now."""
    event = _event(**kwargs)
    event.timestamp += timedelta(seconds=when)
    runtime.start(event)
    return event


def _lane_event(ledger_message_id=None):
    """The fresh event Hermes sends a finished turn's reply under when a follow-up is queued."""
    lane = _event(internal=False)
    lane.message_id, lane.ledger_message_id = None, ledger_message_id
    return lane


def _render(content):
    return [{"type": "section", "text": {"type": "mrkdwn", "text": content}}]


def _buttons(blocks):
    return [e for b in blocks or () if b.get("type") == "actions" for e in b["elements"]]


class FlagOn(unittest.TestCase):
    def setUp(self):
        runtime._marks.clear()
        runtime._carried.clear()
        patcher = mock.patch.dict(os.environ, {FLAG_ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(runtime._marks.clear)
        self.addCleanup(runtime._carried.clear)

    def draw(self, event, content=REPLY, render=_render):
        token = runtime.begin(event)
        try:
            return runtime.maybe_blocks(content, render)
        finally:
            runtime.end(token)


class PresentTest(unittest.TestCase):
    def test_mock_06_gets_a_bold_lead_and_its_question_as_the_offer(self):
        bolded, label = runtime.present(REPLY)
        self.assertTrue(bolded.startswith("**I couldn't find seeded-z.** The fleet has"))
        self.assertTrue(bolded.endswith("Check it there?"))
        self.assertEqual(label, "check it there")

    def test_a_reply_that_ends_on_a_statement_offers_nothing(self):
        bolded, label = runtime.present("The check crashed. It's being retried.")
        self.assertEqual(bolded, "**The check crashed.** It's being retried.")
        self.assertEqual(label, "")

    def test_a_lead_holding_markup_is_left_plain(self):
        for reply in (
            "*Already bold.* Retry?",
            "`seeded-z`. Retry?",
            "`seeded-z` is *gone*. Retry?",
            "An unpaired ` tick. Retry?",
            "- a list item. Retry?",
            "1. Restart the pod.\n2. Check the logs.\nRetry?",
            "2) Check the logs. Retry?",
            "Can't reach `api`'s pods. Retry?",
            "Lost `api`, then retried. Retry?",
            "# A heading. Retry?",
        ):
            with self.subTest(reply=reply):
                bolded, _ = runtime.present(reply)
                self.assertEqual(bolded, reply)

    def test_a_lead_opening_on_an_emoji_a_quote_or_a_bracket_is_bold(self):
        for lead in ("\u26a0\ufe0f The check crashed.", '"seeded-z" is not a cluster.', "(Retrying) The pod is gone."):
            with self.subTest(lead=lead):
                self.assertEqual(
                    runtime.present(f"{lead} Check it there?"), (f"**{lead}** Check it there?", "check it there")
                )

    def test_a_code_span_in_the_lead_stays_code_and_the_words_around_it_are_bold(self):
        cases = {
            "I couldn't find `seeded-z` in the fleet. Check it there?": (
                "**I couldn't find** `seeded-z` **in the fleet.** Check it there?"
            ),
            "`seeded-z` is gone. Retry?": "`seeded-z` **is gone.** Retry?",
            "The pod `api-7f` crashed with `OOMKilled`. More.": (
                "**The pod** `api-7f` **crashed with** `OOMKilled`. More."
            ),
        }
        for reply, want in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[0], want)

    def test_a_question_that_does_not_fit_a_button_or_holds_markup_offers_nothing(self):
        long_question = "Want me to " + "really " * 12 + "try again?"
        for reply in (f"It stopped. {long_question}", "It stopped. Retry on `seeded-a`?", "It stopped?\nNo."):
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[1], "")

    def test_a_question_on_a_list_item_or_heading_offers_nothing(self):
        for reply in (
            "Two options.\n- Retry on seeded-a?\n- Stop here?",
            "It stopped.\n- Stop here?",
            "It stopped.\n2. Stop here?",
            "It stopped.\n## Retry?",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[1], "")

    def test_a_question_that_needs_a_word_offers_nothing(self):
        for question in (
            "What would you like me to do?",
            "Which namespace should it use?",
            "Where should I look next?",
            "How should I proceed?",
            "Retry on seeded-a, or check seeded-b first?",
            "Should I retry or stop?",
            "What\u2019s next?",
            "How's that?",
            "So, which one should I use?",
            "Anything else?",
            "Could you share the namespace?",
            "Can you tell me which cluster it runs on?",
            "Do you know the correct image tag?",
            "Want me to check where it runs?",
        ):
            with self.subTest(question):
                self.assertEqual(runtime.present("It failed. " + question)[1], "")

    def test_a_soft_wrapped_first_sentence_is_bolded_whole_on_its_own_lines(self):
        for reply, bolded in {
            "I couldn't find\nseeded-z. The fleet has seeded-a.\nCheck it there?":
                "**I couldn't find**\n**seeded-z.** The fleet has seeded-a.\nCheck it there?",
            "I couldn't find\nseeded-z.\nThe fleet has seeded-a.":
                "**I couldn't find**\n**seeded-z.**\nThe fleet has seeded-a.",
            "The check stopped on\n- seeded-a\n- seeded-b":
                "**The check stopped on**\n- seeded-a\n- seeded-b",
            "The check stopped\n\nIt will retry.": "**The check stopped**\n\nIt will retry.",
        }.items():
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[0], bolded)

    def test_lines_that_each_start_on_a_capital_stay_lines(self):
        reply = "Deploy failed\nPod: api-7f\nReason: OOMKilled\nRetry with more memory?"
        self.assertEqual(
            runtime.present(reply),
            ("**Deploy failed**\nPod: api-7f\nReason: OOMKilled\nRetry with more memory?", "retry with more memory"),
        )

    def test_a_question_on_its_own_line_after_a_list_or_an_open_line_is_offered(self):
        for reply in (
            "I couldn't find seeded-z. The fleet has:\n- seeded-a\n- seeded-b\n\nCheck it there?",
            "I couldn't find seeded-z. The fleet has:\n- seeded-a\n- seeded-b\nCheck it there?",
            "I couldn't find seeded-z on:\nCheck it there?",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[1], "check it there")

    def test_a_question_after_a_blank_line_is_offered_whatever_it_opens_on(self):
        for reply, want in {
            "It failed.\n\nOK to retry?": "OK to retry",
            "It failed.\n\nI'll retry at 14:00, want that?": "I'll retry at 14:00, want that",
            "It failed.\n\n2 retries left, use one?": "2 retries left, use one",
            "It failed.\n\nretry it on seeded-a?": "retry it on seeded-a",
        }.items():
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[1], want)

    def test_a_question_cut_at_an_abbreviation_offers_nothing(self):
        for reply in (
            "It failed. Scale down vs. roll back?",
            "It failed. Restart it on seeded-a, i.e. the canary?",
            "It failed. Try another cluster, e.g. seeded-a?",
            "It failed. Reopen ticket No. 3?",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[1], "")
        for reply in ("It failed. The answer is no. Retry?", "I checked pods, services, etc.\n\nRetry?"):
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[1], "retry")

    def test_a_question_that_carries_on_the_line_above_offers_nothing(self):
        for reply in ("It stopped. Should I check it\nthere?", "It stopped.\n- seeded-a\n  Should I retry?"):
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[1], "")

    def test_a_lead_wrapped_onto_an_acronym_or_i_is_bolded_whole(self):
        for reply, want in {
            "I couldn't reach the\nAPI server. Should I retry?": "**I couldn't reach the**\n**API server.** Should I retry?",
            "The pod is gone, so\nI stopped. Retry?": "**The pod is gone, so**\n**I stopped.** Retry?",
        }.items():
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply)[0], want)

    def test_a_nul_in_the_reply_is_not_read_as_a_code_span(self):
        self.assertEqual(
            runtime.present("I couldn't find `a.b` or \x000\x00 it. Retry?"),
            ("**I couldn't find** `a.b` **or 0 it.** Retry?", "retry"),
        )

    def test_a_code_span_is_read_as_the_presenter_reads_it(self):
        for reply, want in {
            "The pod says `Back-off restarting. See logs` and exits. Retry?": (
                "**The pod says** `Back-off restarting. See logs` **and exits.** Retry?"
            ),
            "The image ``a`b`` is missing. Retry?": "**The image** ``a`b`` **is missing.** Retry?",
        }.items():
            with self.subTest(reply=reply):
                self.assertEqual(runtime.present(reply), (want, "retry"))

    def test_the_offer_keeps_words_capitalised_anyway(self):
        for question, label in {
            "Retry it?": "retry it",
            "I can retry it?": "I can retry it",
            "I'll retry it?": "I'll retry it",
            "OK to retry?": "OK to retry",
            "A retry?": "a retry",
        }.items():
            with self.subTest(question):
                self.assertEqual(runtime.present("It failed. " + question)[1], label)

    def test_a_lone_question_is_both_lead_and_offer(self):
        self.assertEqual(
            runtime.present("Try again on seeded-a?"), ("**Try again on seeded-a?**", "try again on seeded-a")
        )


class MarkTest(FlagOn):
    def test_a_failure_wake_marks_its_thread_and_its_reply_is_drawn(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        blocks = self.draw(_turn())
        self.assertTrue(blocks[0]["text"]["text"].startswith("**I couldn't find seeded-z.**"))
        (button,) = _buttons(blocks)
        self.assertEqual(button["text"]["text"], "check it there")
        self.assertEqual(button["value"], "check it there")
        self.assertRegex(button["action_id"], slack_presenter.CHOICE_ACTION_ID_PATTERN)

    def test_a_reply_needs_its_turn_to_have_claimed_the_mark(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        self.assertEqual(self.draw(_event()), _render(REPLY))

    def test_a_queued_user_message_drops_the_mark(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, _later(internal=False))
        self.assertEqual(runtime._marks, {})
        self.assertEqual(self.draw(_turn()), _render(REPLY))

    def test_a_user_turn_starting_after_the_mark_drops_it(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.start(_later(internal=False))
        self.assertEqual(self.draw(_turn()), _render(REPLY))

    def test_a_queued_wake_keeps_the_mark(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, _event())
        self.assertEqual(len(_buttons(self.draw(_event()))), 1)

    def test_a_wake_queued_behind_a_user_turn_carries_the_mark_to_its_reply(self):
        # The follow-up's final goes out under the outer turn's event, the user's.
        outer = _event(internal=False)
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, _event())
        blocks = self.draw(outer)
        self.assertTrue(blocks[0]["text"]["text"].startswith("**I couldn't find seeded-z.**"))
        self.assertEqual(len(_buttons(blocks)), 1)
        self.assertEqual(self.draw(_turn()), _render(REPLY))

    def test_a_carried_mark_the_follow_up_never_sent_does_not_draw_a_later_user_turn(self):
        # A [SILENT] or streamed wake reply never reaches the outer send.
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, _event())
        self.assertEqual(self.draw(_later(internal=False)), _render(REPLY))
        self.assertEqual(runtime._carried, {})

    def test_a_carried_mark_outlives_a_second_failure_wake(self):
        outer = _event(internal=False)
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, _event())
        runtime.note_wake(SUB, {"blocked"}, WAKE)
        self.assertEqual(len(_buttons(self.draw(outer))), 1)
        self.assertEqual(len(_buttons(self.draw(_turn()))), 1)

    def test_a_user_message_queued_after_a_carried_wake_drops_the_mark(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, _event())
        runtime.drop(_event().source, _later(internal=False))
        self.assertEqual(self.draw(_event(internal=False)), _render(REPLY))
        self.assertEqual(self.draw(_turn()), _render(REPLY))

    def test_a_wake_with_a_message_queued_behind_it_keeps_its_look(self):
        # U1 runs; the wake and then U2 queue behind it.
        outer = _event(internal=False)
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        self.assertEqual(self.draw(_lane_event(USER_MESSAGE_ID)), _render(REPLY))
        runtime.drop(_event().source, _event())
        self.assertEqual(len(_buttons(self.draw(_lane_event()))), 1)
        runtime.drop(_event().source, _later(internal=False))
        self.assertEqual(self.draw(outer), _render(REPLY))

    def test_a_wake_turn_with_a_user_message_queued_behind_it_keeps_its_look(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        wake = _turn()
        self.assertEqual(len(_buttons(self.draw(_lane_event()))), 1)
        runtime.drop(_event().source, _later(internal=False))
        self.assertEqual(self.draw(wake), _render(REPLY))

    def test_a_user_message_queued_before_the_wake_leaves_its_mark(self):
        outer = _event(internal=False)
        queued = _earlier(internal=False)
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, queued)
        self.assertEqual(self.draw(_lane_event(USER_MESSAGE_ID)), _render(REPLY))
        runtime.drop(_event().source, _event())
        self.assertEqual(len(_buttons(self.draw(outer))), 1)

    def test_a_wake_running_when_a_failure_wake_queues_behind_it_stays_plain(self):
        running = _turn(when=-1)
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        self.assertEqual(self.draw(_lane_event()), _render(REPLY))
        runtime.drop(_event().source, _event())
        self.assertEqual(len(_buttons(self.draw(running))), 1)

    def test_a_wake_queued_ahead_of_a_failure_wake_stays_plain(self):
        outer = _event(internal=False)
        ahead = _earlier()
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, ahead)
        self.assertEqual(self.draw(_lane_event()), _render(REPLY))
        runtime.drop(_event().source, _event())
        self.assertEqual(len(_buttons(self.draw(outer))), 1)

    def test_two_failure_wakes_in_a_row_each_keep_their_look(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        first = _turn()
        runtime.note_wake(SUB, {"crashed"}, WAKE)
        self.assertEqual(len(_buttons(self.draw(_lane_event()))), 1)
        runtime.drop(_event().source, _event())
        self.assertEqual(len(_buttons(self.draw(first))), 1)

    def test_a_follow_up_with_no_queued_event_claims_nothing(self):
        # A /steer or interrupt follow-up runs with no pending event.
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.drop(_event().source, None)
        self.assertEqual(self.draw(_lane_event()), _render(REPLY))
        self.assertEqual(len(_buttons(self.draw(_turn()))), 1)

    def test_a_follow_up_with_no_queued_event_drops_an_unsent_claim(self):
        # The wake's reply was [SILENT]; the /steer reply goes out under the wake's event.
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        wake = _turn()
        runtime.drop(_event().source, None)
        self.assertEqual(self.draw(wake), _render(REPLY))

    def test_the_next_turn_starting_drops_an_unsent_claim(self):
        # The wake's reply was [SILENT]; a sibling wake's turn, or a user message queued
        # before the claim, runs next.
        for internal in (True, False):
            with self.subTest(internal=internal):
                runtime.note_wake(SUB, {"gave_up"}, WAKE)
                queued = _earlier(internal=internal)
                _turn()
                runtime.start(queued)
                self.assertEqual(self.draw(queued), _render(REPLY))

    def test_an_event_with_text_and_no_message_id_does_not_take_a_claim(self):
        # A Slack slash command carries no message id.
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        _turn()
        command = _later(internal=False)
        command.message_id, command.text = None, "/hermes status"
        self.assertEqual(self.draw(command), _render(REPLY))

    def test_a_sibling_wake_leaves_the_mark(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.note_wake(SUB, {"completed"}, "Task t_f2 completed.")
        self.assertEqual(len(_buttons(self.draw(_turn()))), 1)

    def test_outside_a_thread_the_lead_is_bold_and_the_question_stays_text(self):
        runtime.note_wake({**SUB, "thread_id": None}, {"gave_up"}, WAKE)
        blocks = self.draw(_turn(thread_id=None))
        self.assertTrue(blocks[0]["text"]["text"].startswith("**I couldn't find seeded-z.**"))
        self.assertEqual(_buttons(blocks), [])

    def test_a_wake_whose_question_is_posted_clears_the_mark(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.note_wake(SUB, {"blocked"}, WAKE + " " + slack_ux_moments.WAKE_NOTE)
        self.assertEqual(runtime._marks, {})

    def test_a_sibling_wake_whose_question_is_posted_leaves_the_mark(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        runtime.note_wake({**SUB, "task_id": "t_f2"}, {"blocked"}, WAKE + " " + slack_ux_moments.WAKE_NOTE)
        self.assertEqual(len(_buttons(self.draw(_turn()))), 1)

    def test_the_mark_is_taken_once(self):
        runtime.note_wake(SUB, {"blocked"}, WAKE)
        self.draw(_turn())
        self.assertEqual(self.draw(_turn()), _render(REPLY))

    def test_unmarked_sends_render_as_upstream(self):
        cases = {
            "completed wake": ({"completed"}, WAKE, _event()),
            "question already posted": ({"blocked"}, f"{WAKE}\n\n{slack_ux_moments.WAKE_NOTE}", _event()),
            "the user's own message": ({"gave_up"}, WAKE, _event(internal=False)),
            "another thread": ({"gave_up"}, WAKE, _event(thread_id="1700000000.000999")),
            "another platform": ({"gave_up"}, WAKE, _event(platform="google_chat")),
        }
        for name, (kinds, text, event) in cases.items():
            with self.subTest(name):
                runtime._marks.clear()
                runtime.note_wake(SUB, kinds, text)
                event.timestamp = datetime.now() + timedelta(seconds=1)
                runtime.start(event)
                self.assertEqual(self.draw(event), _render(REPLY))

    def test_a_mark_past_its_ttl_is_dropped(self):
        with mock.patch.object(runtime.time, "monotonic", return_value=1000.0):
            runtime.note_wake(SUB, {"gave_up"}, WAKE)
        with mock.patch.object(runtime.time, "monotonic", return_value=1000.0 + runtime.MARK_TTL_SECONDS + 1):
            self.assertEqual(self.draw(_turn()), _render(REPLY))

    def test_marks_are_capped_oldest_first(self):
        for i in range(runtime.MARKS_MAX + 1):
            runtime.note_wake({**SUB, "thread_id": str(i)}, {"gave_up"}, WAKE)
        self.assertEqual(len(runtime._marks), runtime.MARKS_MAX)
        self.assertNotIn(("C0KAGE", "0"), runtime._marks)

    def test_a_render_that_declines_keeps_upstreams_answer(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        self.assertIsNone(self.draw(_turn(), render=lambda content: None))

    def test_a_message_at_the_block_cap_keeps_its_question_as_text(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        full = lambda content: _render(content) * runtime.MESSAGE_BLOCKS_MAX  # noqa: E731
        self.assertEqual(_buttons(self.draw(_turn(), render=full)), [])

    def test_a_render_that_raises_falls_back_to_the_reply_as_written(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        seen = []

        def render(content):
            seen.append(content)
            if content != REPLY:
                raise ValueError("boom")
            return _render(content)

        with self.assertLogs(runtime.logger, "WARNING"):
            self.assertEqual(self.draw(_turn(), render=render), _render(REPLY))

    def test_the_mark_does_not_outlive_the_send(self):
        runtime.note_wake(SUB, {"gave_up"}, WAKE)
        self.draw(_turn())
        self.assertFalse(runtime._marked.get())


class FlagOff(unittest.TestCase):
    def test_nothing_is_marked(self):
        runtime._marks.clear()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(FLAG_ENV, None)
            runtime.note_wake(SUB, {"gave_up"}, WAKE)
        self.assertEqual(runtime._marks, {})
        self.assertIsNone(runtime.begin(_event()))


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        files = {
            applier.NOTIFIER: "class N:\n    def build_wake_text(self):\n" + applier.WAKE_ANCHOR,
            applier.BASE: "class B:\n    async def send_final_ledgered(self, event):\n"
            + applier.FINAL_ANCHOR
            + "        return result\n\n"
            + "    async def _process_message_background(self, event, session_key):\n"
            + "        try:\n"
            + applier.START_ANCHOR
            + "        finally:\n"
            + "            pass\n",
            applier.SLACK_ADAPTER: "from typing import Optional\n\n\nclass SlackAdapter:\n"
            + applier.BLOCKS_ANCHOR
            + "        return None\n",
            applier.RUN_TURN: "class R:\n    async def _run_agent_queued_followup(self, turn_ctx, pending_event):\n"
            + applier.FOLLOWUP_ANCHOR
            + "        return None\n",
        }
        for rel, text in files.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)

    def test_each_file_gets_its_call_and_a_second_run_is_refused(self):
        applier.apply(self.root)
        for rel in (applier.NOTIFIER, applier.BASE, applier.SLACK_ADAPTER, applier.RUN_TURN):
            self.assertIn(applier.BUILD_MARKER, (self.root / rel).read_text())
        self.assertIn("def _kage_upstream_maybe_blocks(", (self.root / applier.SLACK_ADAPTER).read_text())
        verifier.check_callers(self.root)
        with self.assertRaises(SystemExit):
            applier.apply(self.root)

    def test_the_verifier_refuses_a_call_whose_import_is_gone(self):
        applier.apply(self.root)
        imported = applier.GATEWAY_IMPORT.strip().splitlines()[-1]
        for rel in (applier.NOTIFIER, applier.BASE, applier.SLACK_ADAPTER, applier.RUN_TURN):
            with self.subTest(rel=rel):
                path = self.root / rel
                patched = path.read_text()
                path.write_text(patched.replace(imported, ""))
                with self.assertRaises(SystemExit) as ctx:
                    verifier.check_callers(self.root)
                self.assertIn("does not import gateway.slack_ux_failure", str(ctx.exception))
                path.write_text(patched)

    def test_the_verifier_refuses_a_call_left_only_in_a_comment(self):
        applier.apply(self.root)
        path = self.root / applier.BASE
        call = "_kage_slack_failure.start(event)"
        path.write_text(path.read_text().replace(call, f"pass  # {call}"))
        with self.assertRaises(SystemExit) as ctx:
            verifier.check_callers(self.root)
        self.assertIn(f"_process_message_background does not make {call!r}", str(ctx.exception))

    def test_the_verifier_refuses_a_call_made_in_another_function(self):
        applier.apply(self.root)
        path = self.root / applier.BASE
        path.write_text(path.read_text().replace("_process_message_background", "_elsewhere"))
        with self.assertRaises(SystemExit) as ctx:
            verifier.check_callers(self.root)
        self.assertIn("defines no _process_message_background", str(ctx.exception))

    def test_the_verifier_refuses_a_call_out_of_its_place(self):
        start = "            _kage_slack_failure.start(event)\n"
        end = "            _kage_slack_failure.end(_kage_failure_token)\n"
        begin = "        _kage_failure_token = _kage_slack_failure.begin(event)\n"
        moved = {
            "start above its hook": (
                applier.BASE,
                lambda text: text.replace(start, "").replace(applier.START_ANCHOR, start + applier.START_ANCHOR),
            ),
            "start under an if": (applier.BASE, lambda text: text.replace(start, "            if False:\n    " + start)),
            "end outside the finally": (
                applier.BASE,
                lambda text: text.replace(end, "            pass\n" + end.replace("    ", "", 1)),
            ),
            "begin below the send": (
                applier.BASE,
                lambda text: text.replace(begin, "").replace("        finally:\n" + end, "    " + begin + "        finally:\n" + end),
            ),
            "note_wake apart from the moments line": (
                applier.NOTIFIER,
                lambda text: text.replace(applier.WAKE_ANCHOR, applier.WAKE_ANCHOR + "        pass\n"),
            ),
        }
        for name, (rel, misplace) in moved.items():
            with self.subTest(name=name):
                self.setUp()
                applier.apply(self.root)
                path = self.root / rel
                text = path.read_text()
                self.assertNotEqual(misplace(text), text)
                path.write_text(misplace(text))
                with self.assertRaises(SystemExit) as ctx:
                    verifier.check_callers(self.root)
                self.assertIn("does not make", str(ctx.exception))

    def test_the_verifier_reads_maybe_blocks_on_the_slack_adapter_only(self):
        applier.apply(self.root)
        path = self.root / applier.SLACK_ADAPTER
        patched = path.read_text()
        upstream = patched.replace("class SlackAdapter:", "class Other:") + (
            "\n\nclass SlackAdapter:\n" + applier.BLOCKS_ANCHOR + "        return None\n"
            "\n    def _kage_upstream_maybe_blocks(self, content):\n        return None\n"
        )
        path.write_text(upstream)
        with self.assertRaises(SystemExit) as ctx:
            verifier.check_callers(self.root)
        self.assertIn("SlackAdapter._maybe_blocks does not make", str(ctx.exception))

    def test_the_verifier_refuses_a_call_reading_a_name_nothing_binds(self):
        path = self.root / applier.RUN_TURN
        path.write_text(path.read_text().replace("(self, turn_ctx, pending_event)", "(self, ctx, pending_event)"))
        applier.apply(self.root)
        with self.assertRaises(SystemExit) as ctx:
            verifier.check_callers(self.root)
        self.assertIn("turn_ctx", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
