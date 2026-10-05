"""Host tests for the KAGE_SLACK_UX moments module. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_moments.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import apply_slack_ux_moments as applier
import slack_ux_clicks as clicks
import slack_ux_moments as runtime
import verify_slack_ux_moments as verifier

PR = "https://github.com/acme/fleet-config/pull/412"
SUB = {
    "task_id": "t_e0c1",
    "platform": "slack",
    "chat_id": "C0KAGE",
    "thread_id": "1700000000.000100",
    "team_id": "T1",
}
QUESTION = {"kind": "needs_input", "reason": "Which cluster?\n- seeded-a\n- seeded-b"}
POSTED_TS = "1700000000.000300"
EARLIER_TS = "1700000000.000200"
WAKE = "Task t_e0c1 is blocked.\n\ngateway.kanban.wake.guidance"

#: build_wake_text's shape upstream, trimmed to what the patch touches.
NOTIFIER = """\
def t(key, **kwargs):
    return key


class _KanbanNotification:
    def __init__(self, sub, events, wake_kinds):
        self.sub = sub
        self.d = {"events": events}
        self.wake_kinds = wake_kinds

    def build_wake_text(self) -> None:
        synth = "Task t_e0c1 is blocked."
        self.synth = synth + "\\n\\n" + t("gateway.kanban.wake.guidance")
"""


class _Client:
    def __init__(self, adapter):
        self.adapter = adapter

    async def chat_postMessage(self, **kwargs):
        if self.adapter.fail:
            raise RuntimeError("channel_not_found")
        self.adapter.posts.append(kwargs)
        return {"ts": self.adapter.post_ts}

    async def chat_update(self, **kwargs):
        if self.adapter.fail or self.adapter.fail_update:
            raise RuntimeError("message_not_found")
        self.adapter.updates.append(kwargs)

    async def conversations_replies(self, **kwargs):
        self.adapter.reads.append(kwargs)
        if isinstance(self.adapter.replies, Exception):
            raise self.adapter.replies
        return {"messages": self.adapter.replies}


class _Adapter:
    def __init__(self, fail=False):
        self.posts = []
        self.updates = []
        self.teams = []
        self.fail = fail
        self.fail_update = False
        self.post_ts = POSTED_TS
        self.replies = []
        self.reads = []
        self.unauthorized = set()
        self.names = {"U7": "Priya"}
        # No bot id yet: the channel gate is skipped, as the clicks module skips it.
        self._bot_user_id = ""
        self._team_bot_user_ids = {}
        self.gated = []

    def _slack_message_matches_mention_patterns(self, text):
        return False

    async def _channel_gate_allows(self, *, channel_id, routing_text, bot_uid, is_mentioned, is_thread_reply,
                                   event_thread_ts, user_id, team_id, is_dm, force_process):
        """A gate set to require a mention in threads."""
        self.gated.append({"routing_text": routing_text, "is_mentioned": is_mentioned})
        return is_mentioned

    async def _resolve_user_name(self, user_id, chat_id="", team_id=""):
        return self.names.get(user_id, user_id)

    def _event_declares_bot_sender(self, event):
        return False

    def _is_interactive_user_authorized(self, user, channel_id="", team_id=""):
        return user not in self.unauthorized

    def _get_client(self, chat_id, team_id=None):
        self.teams.append(team_id)
        return _Client(self)


def _slack_mention_detection_text(event):
    """adapter.py's helper, which the clicks module finds through the gate's globals."""
    return event.get("text", "")


def _event(event_id, kind="blocked"):
    return SimpleNamespace(id=event_id, kind=kind)


def _buttons(blocks):
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _run(coro):
    return asyncio.run(coro)


class EnabledTest(unittest.TestCase):
    def test_off_unless_the_flag_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(runtime.enabled())
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}):
            self.assertTrue(runtime.enabled())

    def test_off_when_the_renderer_is_missing(self):
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}), mock.patch.object(runtime, "_moments", None):
            self.assertFalse(runtime.enabled())


class PrOpenedTest(unittest.TestCase):
    def setUp(self):
        runtime._announced.clear()

    def test_posts_in_the_cards_thread_once_per_pr(self):
        adapter = _Adapter()
        self.assertTrue(_run(runtime.pr_opened(adapter, SUB, f"Opened {PR}")))
        self.assertFalse(_run(runtime.pr_opened(adapter, SUB, f"Report: opened {PR}")))
        self.assertEqual(len(adapter.posts), 1)
        post = adapter.posts[0]
        self.assertEqual((post["channel"], post["thread_ts"]), ("C0KAGE", "1700000000.000100"))
        self.assertEqual(adapter.teams, ["T1"])
        self.assertIn("PR #412", post["text"])

    def test_another_channel_gets_its_own(self):
        adapter = _Adapter()
        _run(runtime.pr_opened(adapter, SUB, f"Opened {PR}"))
        _run(runtime.pr_opened(adapter, {**SUB, "chat_id": "C0OTHER"}, f"Opened {PR}"))
        self.assertEqual([p["channel"] for p in adapter.posts], ["C0KAGE", "C0OTHER"])

    def test_another_thread_gets_its_own(self):
        adapter = _Adapter()
        _run(runtime.pr_opened(adapter, SUB, f"Opened {PR}"))
        _run(runtime.pr_opened(adapter, {**SUB, "thread_id": "1700000000.000900"}, f"Opened {PR}"))
        self.assertEqual([p["thread_ts"] for p in adapter.posts], [SUB["thread_id"], "1700000000.000900"])

    def test_opened_by_someone_else_posts_nothing(self):
        adapter = _Adapter()
        self.assertFalse(_run(runtime.pr_opened(adapter, SUB, f"regression came from {PR}, opened by bob")))
        self.assertEqual(adapter.posts, [])

    def test_a_cited_pr_posts_nothing(self):
        adapter = _Adapter()
        self.assertFalse(_run(runtime.pr_opened(adapter, SUB, f"{PR} already covers it")))
        self.assertEqual(adapter.posts, [])

    def test_a_failed_post_is_retried_next_time(self):
        adapter = _Adapter(fail=True)
        self.assertFalse(_run(runtime.pr_opened(adapter, SUB, f"Opened {PR}")))
        adapter.fail = False
        self.assertTrue(_run(runtime.pr_opened(adapter, SUB, f"Opened {PR}")))

    def test_no_channel_or_no_client_posts_nothing(self):
        self.assertFalse(_run(runtime.pr_opened(_Adapter(), {**SUB, "chat_id": ""}, f"Opened {PR}")))
        self.assertFalse(_run(runtime.pr_opened(object(), SUB, f"Opened {PR}")))

    def test_each_pr_a_text_opened_posts_once(self):
        adapter = _Adapter()
        other = "https://github.com/acme/other/pull/7"
        self.assertTrue(_run(runtime.pr_opened(adapter, SUB, f"Opened {PR}")))
        self.assertTrue(_run(runtime.pr_opened(adapter, SUB, f"- Opened {PR}\n- Opened {other}")))
        self.assertFalse(_run(runtime.pr_opened(adapter, SUB, f"- Opened {PR}\n- Opened {other}")))
        self.assertEqual([("PR #412" in p["text"], "PR #7" in p["text"]) for p in adapter.posts], [(True, False), (False, True)])

    def test_the_announced_map_is_bounded(self):
        adapter = _Adapter()
        with mock.patch.object(runtime, "ANNOUNCED_MAX", 2):
            for n in range(3):
                _run(runtime.pr_opened(adapter, SUB, f"Opened {PR[:-3]}{n}"))
        self.assertEqual(len(runtime._announced), 2)


class NeedsYouTest(unittest.TestCase):
    def setUp(self):
        runtime._questions.clear()
        runtime._unsettled.clear()

    def test_posts_a_needs_input_question_with_its_choices(self):
        adapter = _Adapter()
        self.assertTrue(_run(runtime.needs_you(adapter, SUB, QUESTION)))
        blocks = adapter.posts[0]["blocks"]
        labels = [e["text"]["text"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        self.assertEqual(labels, ["seeded-a", "seeded-b"])
        self.assertEqual(adapter.posts[0]["thread_ts"], SUB["thread_id"])

    def test_the_question_text_names_its_card_above_the_choices(self):
        # A typed answer can open a session the wake never reached; it reads the card here.
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION))
        lines = adapter.posts[0]["text"].split("\n")
        self.assertEqual(lines[-2], "(Question from card t_e0c1.)")
        self.assertTrue(lines[-1].startswith("Reply with one of: "), lines)
        self.assertNotIn("t_e0c1", str(adapter.posts[0]["blocks"]))

    def test_a_question_with_no_thread_names_its_card_last(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, {**SUB, "thread_id": ""}, QUESTION))
        self.assertTrue(adapter.posts[0]["text"].endswith("- seeded-b\n(Question from card t_e0c1.)"))

    def test_a_detail_line_reading_like_the_choices_line_keeps_the_card_last(self):
        adapter = _Adapter()
        payload = {"kind": "needs_input", "reason": "Blocked.\nDetails.\n\nReply with one of: yes or no"}
        _run(runtime.needs_you(adapter, {**SUB, "thread_id": ""}, payload))
        self.assertTrue(adapter.posts[0]["text"].endswith("yes or no\n(Question from card t_e0c1.)"))

    def test_other_kinds_and_empty_reasons_post_nothing(self):
        adapter = _Adapter()
        for payload in ({**QUESTION, "kind": "capability"}, {"kind": "needs_input", "reason": ""}, None, "x"):
            self.assertFalse(_run(runtime.needs_you(adapter, SUB, payload)), payload)
        self.assertEqual(adapter.posts, [])

    def test_a_failed_post_returns_false(self):
        self.assertFalse(_run(runtime.needs_you(_Adapter(fail=True), SUB, QUESTION, 3)))
        self.assertEqual(runtime._questions, {})

    def test_no_thread_posts_the_options_as_text(self):
        adapter = _Adapter()
        self.assertTrue(_run(runtime.needs_you(adapter, {**SUB, "thread_id": ""}, QUESTION)))
        post = adapter.posts[0]
        self.assertIsNone(post["thread_ts"])
        self.assertEqual(_buttons(post["blocks"]), [])
        self.assertIn("- seeded-a\n- seeded-b", post["text"])

    def test_blocking_again_does_not_settle_the_earlier_question_itself(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        _run(runtime.needs_you(adapter, SUB, QUESTION, 9))
        self.assertEqual((len(adapter.posts), adapter.updates), (2, []))

    def test_blocking_again_after_the_callers_settle_replaces_the_question(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        _run(runtime.settle_question(adapter, SUB))
        _run(runtime.needs_you(adapter, SUB, QUESTION, 9))
        self.assertEqual(len(adapter.posts), 2)
        self.assertEqual(len(adapter.updates), 1)
        self.assertEqual(runtime._questions[runtime._sub_key(SUB)][0], 9)

    def test_a_failed_re_ask_leaves_the_earlier_question_to_settle_once(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.fail = True
        self.assertFalse(_run(runtime.needs_you(adapter, SUB, QUESTION, 9)))
        self.assertEqual(runtime._unsettled, {})
        self.assertEqual(runtime._questions[runtime._sub_key(SUB)][0], 3)
        adapter.fail = False
        _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(len(adapter.updates), 1)

    def test_a_replayed_block_neither_settles_nor_reposts(self):
        adapter = _Adapter()
        self.assertTrue(_run(runtime.needs_you(adapter, SUB, QUESTION, 3)))
        self.assertTrue(runtime.asked(SUB, 3))
        self.assertFalse(runtime.asked(SUB, 4))
        self.assertFalse(runtime.asked({**SUB, "task_id": "t_other"}, 3))
        self.assertTrue(_run(runtime.needs_you(adapter, SUB, QUESTION, 3)))
        self.assertEqual((len(adapter.posts), adapter.updates), (1, []))
        self.assertTrue(runtime.asked(SUB, 3), "the replay dropped the open question")

    def test_a_question_with_no_event_id_is_never_a_replay(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION))
        self.assertFalse(runtime.asked(SUB, 0))
        _run(runtime.needs_you(adapter, SUB, QUESTION))
        self.assertEqual(len(adapter.posts), 2)

    def test_the_card_is_found_by_its_open_questions_message(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        self.assertEqual(runtime.question_card("C0KAGE", POSTED_TS), SUB["task_id"])
        self.assertIsNone(runtime.question_card("C0OTHER", POSTED_TS))
        self.assertIsNone(runtime.question_card("C0KAGE", "1700000000.000999"))
        _run(runtime.settle_question(adapter, SUB))
        self.assertIsNone(runtime.question_card("C0KAGE", POSTED_TS))


class WakeTextTest(unittest.TestCase):
    """The ``blocked`` wake notes a question needs_you posted for that event, and nothing else."""

    def setUp(self):
        runtime._questions.clear()

    def test_unchanged_when_no_question_was_posted(self):
        self.assertIs(runtime.wake_text(SUB, [_event(3)], {"blocked"}, WAKE), WAKE)

    def test_notes_the_question_posted_for_this_event(self):
        _run(runtime.needs_you(_Adapter(), SUB, QUESTION, 3))
        noted = runtime.wake_text(SUB, [_event(2, "heartbeat"), _event(3)], {"blocked"}, WAKE)
        self.assertEqual(noted, f"{WAKE}\n\n{runtime.WAKE_NOTE} {runtime.WAKE_NOTE_ANSWERED}")
        self.assertIn("[SILENT]", noted)
        self.assertIn("kanban_comment", noted)
        self.assertIn("kanban_unblock", noted)

    def test_a_question_outside_a_thread_is_not_followed_by_silence(self):
        sub = {**SUB, "thread_id": ""}
        _run(runtime.needs_you(_Adapter(), sub, QUESTION, 3))
        noted = runtime.wake_text(sub, [_event(3)], {"blocked"}, WAKE)
        self.assertEqual(noted, f"{WAKE}\n\n{runtime.WAKE_NOTE}")

    def test_a_retried_wake_is_noted_again(self):
        _run(runtime.needs_you(_Adapter(), SUB, QUESTION, 3))
        first = runtime.wake_text(SUB, [_event(3)], {"blocked"}, WAKE)
        self.assertEqual(runtime.wake_text(SUB, [_event(3)], {"blocked"}, WAKE), first)

    def test_unchanged_for_another_event_card_or_wake_kind(self):
        _run(runtime.needs_you(_Adapter(), SUB, QUESTION, 3))
        self.assertIs(runtime.wake_text(SUB, [_event(4)], {"blocked"}, WAKE), WAKE)
        self.assertIs(runtime.wake_text({**SUB, "task_id": "t_other"}, [_event(3)], {"blocked"}, WAKE), WAKE)
        self.assertIs(runtime.wake_text(SUB, [_event(3, "crashed")], {"crashed"}, WAKE), WAKE)

    def test_unchanged_once_the_question_is_settled(self):
        _run(runtime.needs_you(_Adapter(), SUB, QUESTION, 3))
        _run(runtime.settle_question(_Adapter(), SUB))
        self.assertIs(runtime.wake_text(SUB, [_event(3)], {"blocked"}, WAKE), WAKE)

    def test_a_question_posted_with_no_event_id_is_not_noted(self):
        _run(runtime.needs_you(_Adapter(), SUB, QUESTION))
        self.assertIs(runtime.wake_text(SUB, [_event(0)], {"blocked"}, WAKE), WAKE)

    def test_bad_input_returns_the_text(self):
        _run(runtime.needs_you(_Adapter(), SUB, QUESTION, 3))
        self.assertIs(runtime.wake_text(SUB, [SimpleNamespace(id="x", kind="blocked")], {"blocked"}, WAKE), WAKE)


class SettleQuestionTest(unittest.TestCase):
    """A card that moves on takes the buttons and "waiting on you" off its question."""

    def setUp(self):
        runtime._questions.clear()
        runtime._unsettled.clear()
        runtime._resumed.clear()
        runtime._credited.clear()
        # The image installs the clicks module as gateway.slack_ux_clicks; the answered line comes from it.
        gateway = mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_clicks=clicks),
                                                "gateway.slack_ux_clicks": clicks})
        gateway.start()
        self.addCleanup(gateway.stop)

    def test_rewrites_the_question_without_buttons_or_the_waiting_line(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        _run(runtime.settle_question(adapter, SUB))
        update = adapter.updates[0]
        self.assertEqual((update["channel"], update["ts"]), ("C0KAGE", POSTED_TS))
        self.assertEqual(_buttons(update["blocks"]), [])
        self.assertNotIn(runtime._moments.WAITING, str(update["blocks"]))
        self.assertIn("Which cluster?", str(update["blocks"]))
        self.assertEqual(runtime._questions, {})

    def test_the_settled_text_drops_the_reply_with_line(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        self.assertIn(runtime._presenter.CHOICES_LEAD, adapter.posts[0]["text"])
        _run(runtime.settle_question(adapter, SUB))
        text = adapter.updates[0]["text"]
        self.assertNotIn(runtime._presenter.CHOICES_LEAD, text)
        self.assertIn("Which cluster?", text)
        self.assertIn("t_e0c1", text.splitlines()[-1])

    def test_once(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        _run(runtime.settle_question(adapter, SUB))
        _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(len(adapter.updates), 1)

    def test_a_typed_answer_leaves_the_line_a_click_would(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [
            {"ts": SUB["thread_id"], "user": "U7", "text": "is checkout-gateway restarting?"},
            {"ts": POSTED_TS, "user": "U0BOT", "bot_id": "B1", "text": "Which cluster?"},
            {"ts": "1700000000.000500", "user": "U8", "text": "seeded-a, I think"},
            {"ts": "1700000000.000400", "user": "U0BOT", "bot_id": "B1", "text": "On it."},
            {"ts": "1700000000.000450", "user": "U7", "subtype": "channel_join", "text": "joined"},
            {"ts": "1700000000.000460", "user": "U7", "text": "seeded-b\n  please"},
        ]
        _run(runtime.settle_question(adapter, SUB))
        update = adapter.updates[0]
        self.assertEqual(update["blocks"][-1], {
            "type": "context", "elements": [{"type": "mrkdwn", "text": "✓ Priya: seeded-b please"}]})
        head, _sep, rest = update["text"].partition("\n\n")
        self.assertEqual(head, "✓ Priya: seeded-b please")
        self.assertIn("Which cluster?", rest)
        self.assertEqual(adapter.reads, [
            {"channel": "C0KAGE", "ts": SUB["thread_id"], "oldest": POSTED_TS, "limit": runtime.REPLIES_READ_MAX}])

    def test_a_typed_answer_is_named_and_clipped_as_a_click_is(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-b " + "word " * 40}]

        async def clicker_name(adapter_, body, user, channel, team_id):
            return f"name-of-{user}-in-{channel}-{team_id}"

        async def hears(*args):
            return True

        clicks = SimpleNamespace(clicker_name=clicker_name, answered=lambda channel, ts: False,
                                 clicked=lambda channel, ts: False, _gateway_hears=hears)
        with mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_clicks=clicks), "gateway.slack_ux_clicks": clicks}):
            _run(runtime.settle_question(adapter, SUB))
        note = adapter.updates[0]["blocks"][-1]["elements"][0]["text"]
        who, _sep, words = note.partition(": ")
        self.assertEqual(who, "✓ name-of-U7-in-C0KAGE-T1")
        self.assertLessEqual(len(words), runtime.TYPED_ANSWER_MAX)
        self.assertTrue(words.startswith("seeded-b word"))

    def test_a_typed_answer_falls_back_to_its_profile_then_someone_never_a_mention(self):
        for case, profile, modules, name in (
            ("the reply's profile", {"display_name": "", "real_name": "Priya R"}, None, "Priya R"),
            ("no profile", None, None, "Someone"),
            # Without the clicks module the reply cannot be put through the channel gate, so it is not counted.
            ("no clicks module", {"real_name": "Priya R"},
             {"gateway": SimpleNamespace(), "gateway.slack_ux_clicks": None}, None),
        ):
            with self.subTest(case=case):
                runtime._questions.clear()
                runtime._credited.clear()
                adapter = _Adapter()
                adapter.names = {}
                _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
                reply = {"ts": "1700000000.000400", "user": "U7", "text": "seeded-b"}
                if profile is not None:
                    reply["user_profile"] = profile
                adapter.replies = [reply]
                with mock.patch.dict(sys.modules, modules or {}):
                    _run(runtime.settle_question(adapter, SUB))
                update = adapter.updates[0]
                if name is None:
                    self.assertNotIn("✓", update["text"])
                else:
                    self.assertEqual(update["blocks"][-1]["elements"][0]["text"], f"✓ {name}: seeded-b")
                self.assertNotIn("<@", update["text"])

    def test_only_a_card_that_resumed_credits_a_typed_reply(self):
        # Upstream sends no "claimed" event to a subscriber, so it is not one of them.
        self.assertEqual(runtime.ANSWERED_KINDS, {"unblocked", "heartbeat"})
        for kind in ("archived", "status", "completed", "gave_up", "unblocked", "claimed", "heartbeat"):
            with self.subTest(kind=kind):
                runtime._questions.clear()
                runtime._credited.clear()
                adapter = _Adapter()
                _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
                adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "hold on, checking"}]
                _run(runtime.settle_question(adapter, SUB, kind))
                update = adapter.updates[0]
                self.assertEqual(_buttons(update["blocks"]), [])
                self.assertEqual("✓" in update["text"], kind in runtime.ANSWERED_KINDS)
                self.assertEqual(bool(adapter.reads), kind in runtime.ANSWERED_KINDS)
                self.assertEqual(runtime._questions, {})

    def test_an_event_older_than_the_question_leaves_it_alone(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 8))
        for event_id in (6, 8):
            _run(runtime.settle_question(adapter, SUB, "unblocked", event_id))
        self.assertEqual(adapter.updates, [])
        _run(runtime.settle_question(adapter, SUB, "unblocked", 9))
        self.assertEqual(_buttons(adapter.updates[0]["blocks"]), [])
        self.assertEqual(runtime._questions, {})

    def test_only_the_agents_mentions_a_reply_opens_with_are_dropped_from_its_line(self):
        for text, words in (
            ("<@U0BOT> <@U9|sam>  seeded-b", "sam seeded-b"),
            ("<@U0BOT>: <@U0BOT> seeded-b", "seeded-b"),
            ("<@U9|sam> <@U0BOT> seeded-b", "sam @U0BOT seeded-b"),
            ("<@U0BOT> seeded-b, ask <@U9|sam>", "seeded-b, ask sam"),
            ("<@U0BOT>", "@U0BOT"),
        ):
            with self.subTest(text=text):
                runtime._questions.clear()
                runtime._credited.clear()
                adapter = _Adapter()
                adapter._bot_user_id = "U0BOT"
                _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
                adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": text}]
                _run(runtime.settle_question(adapter, SUB))
                self.assertEqual(adapter.updates[0]["blocks"][-1]["elements"][0]["text"], f"✓ Priya: {words}")

    def test_a_click_during_the_read_keeps_its_own_line(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-b"}]
        with mock.patch.object(clicks, "answered", lambda channel, ts: bool(adapter.reads)):
            _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(len(adapter.reads), 1)
        self.assertEqual(adapter.updates, [])
        self.assertEqual(runtime._questions, {})

    def test_a_click_whose_rewrite_failed_during_the_read_drops_the_typed_line(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-a"}]
        with mock.patch.object(clicks, "clicked", lambda channel, ts: bool(adapter.reads)), \
                mock.patch.object(clicks, "rewriting", lambda channel, ts: False):
            _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(len(adapter.reads), 1)
        self.assertEqual(_buttons(adapter.updates[0]["blocks"]), [])
        self.assertNotIn("✓", adapter.updates[0]["text"])

    def test_a_click_still_rewriting_is_left_to_its_rewrite(self):
        # Before the read, or landing during it: the settle sends nothing and keeps the question.
        for before in (True, False):
            with self.subTest(before=before):
                runtime._questions.clear()
                runtime._credited.clear()
                adapter = _Adapter()
                _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
                adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-a"}]
                with mock.patch.object(clicks, "clicked", lambda channel, ts: before or bool(adapter.reads)), \
                        mock.patch.object(clicks, "rewriting", lambda channel, ts: before or bool(adapter.reads)):
                    _run(runtime.settle_question(adapter, SUB))
                self.assertEqual(adapter.updates, [])
                self.assertEqual(len(adapter.reads), 0 if before else 1)
                self.assertEqual(len(runtime._questions), 1, "the next event settles it if the rewrite fails")

    def test_an_event_older_than_a_retried_question_leaves_it_alone(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 8))
        _run(runtime.needs_you(adapter, SUB, QUESTION, 9))
        self.assertEqual(len(runtime._unsettled), 1, "the first question waits for a retry")
        _run(runtime.settle_question(adapter, SUB, "unblocked", 8))
        self.assertEqual(adapter.updates, [])
        self.assertEqual(len(runtime._unsettled), 1)

    def test_a_retried_settle_still_reads_the_answer_once_the_card_resumed(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-b"}]
        adapter.fail_update = True
        _run(runtime.settle_question(adapter, SUB, "unblocked", 4))
        adapter.fail_update = False
        self.assertEqual(adapter.updates, [])
        _run(runtime.settle_question(adapter, SUB, "completed", 5))
        self.assertEqual(adapter.updates[0]["blocks"][-1]["elements"][0]["text"], "✓ Priya: seeded-b")
        self.assertEqual(runtime._resumed, {})

    def test_one_reply_answers_one_question_of_a_thread(self):
        # Two cards ask in one thread; the reply is shown on the first to resume, never on both.
        adapter = _Adapter()
        other = {**SUB, "task_id": "t_f1d2"}
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.post_ts = "1700000000.000350"
        _run(runtime.needs_you(adapter, other, QUESTION, 4))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-b"}]
        _run(runtime.settle_question(adapter, other, "unblocked", 5))
        _run(runtime.settle_question(adapter, SUB, "unblocked", 6))
        self.assertEqual([(u["ts"], "✓" in u["text"]) for u in adapter.updates],
                         [("1700000000.000350", True), (POSTED_TS, False)])

    def test_a_reply_not_shown_is_free_for_another_question(self):
        adapter = _Adapter()
        other = {**SUB, "task_id": "t_f1d2"}
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.post_ts = "1700000000.000350"
        _run(runtime.needs_you(adapter, other, QUESTION, 4))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-b"}]
        adapter.fail_update = True
        _run(runtime.settle_question(adapter, other, "unblocked", 5))
        adapter.fail_update = False
        _run(runtime.settle_question(adapter, SUB, "unblocked", 6))
        self.assertEqual([(u["ts"], "✓" in u["text"]) for u in adapter.updates], [(POSTED_TS, True)])

    def test_a_retried_question_reads_only_the_replies_before_the_card_asked_again(self):
        # The reply after the card's next question answers that one, not the question whose settle failed.
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.fail_update = True
        _run(runtime.settle_question(adapter, SUB, "unblocked", 4))
        adapter.fail_update = False
        adapter.post_ts = "1700000000.000350"
        _run(runtime.needs_you(adapter, SUB, QUESTION, 5))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-b"}]
        _run(runtime.settle_question(adapter, SUB, "unblocked", 6))
        self.assertEqual([(u["ts"], "✓" in u["text"]) for u in adapter.updates],
                         [(POSTED_TS, False), ("1700000000.000350", True)])

    def test_a_typed_reply_the_channel_gate_drops_is_not_the_answer(self):
        adapter = _Adapter()
        adapter._bot_user_id = "U0BOT"
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [
            {"ts": "1700000000.000400", "user": "U7", "text": "hmm, seeded-a?"},
            {"ts": "1700000000.000500", "user": "U7", "text": "<@U0BOT> seeded-b"},
        ]
        _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(adapter.updates[0]["blocks"][-1]["elements"][0]["text"], "✓ Priya: seeded-b")
        self.assertEqual([g["routing_text"] for g in adapter.gated], ["hmm, seeded-a?", "<@U0BOT> seeded-b"])

    def test_no_reply_or_a_failed_read_settles_without_the_line(self):
        for replies in ([], [{"ts": "1700000000.000400", "user": "U0BOT", "bot_id": "B1", "text": "On it."}],
                        RuntimeError("missing_scope")):
            with self.subTest(replies=replies):
                runtime._questions.clear()
                runtime._credited.clear()
                adapter = _Adapter()
                _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
                adapter.replies = replies
                _run(runtime.settle_question(adapter, SUB))
                update = adapter.updates[0]
                self.assertNotEqual(update["blocks"][-1]["type"], "context")
                self.assertNotIn("✓", update["text"])
                self.assertEqual(runtime._questions, {})

    def test_a_typed_answer_counts_only_a_person_the_adapter_answers(self):
        adapter = _Adapter()
        adapter.unauthorized = {"U9"}
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [
            {"ts": "1700000000.000400", "user": "U9", "text": "+1 same here"},
            {"ts": "1700000000.000410", "user": "U7", "subtype": "message_deleted", "text": "seeded-a"},
            {"ts": "1700000000.000420", "user": "U7", "subtype": "thread_broadcast", "text": "seeded-b"},
        ]
        _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(adapter.updates[0]["blocks"][-1]["elements"][0]["text"], "✓ Priya: seeded-b")

    def test_a_typed_answer_shows_slack_entities_as_plain_text(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7",
                            "text": "<!channel> <https://example.com/x|seeded-b> &amp; <@U8> &lt;b&gt;"}]
        _run(runtime.settle_question(adapter, SUB))
        note = adapter.updates[0]["blocks"][-1]["elements"][0]["text"]
        self.assertEqual(note, "✓ Priya: !channel seeded-b &amp; @U8 &lt;b&gt;")

    def test_a_mention_inside_a_typed_answer_reads_as_the_name(self):
        adapter = _Adapter()
        adapter.names["U9"] = "Sam | <ops>"
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "seeded-b, ask <@U9> or <@U7> or <@U8>"}]
        _run(runtime.settle_question(adapter, SUB))
        note = adapter.updates[0]["blocks"][-1]["elements"][0]["text"]
        self.assertEqual(note, "✓ Priya: seeded-b, ask @Sam ops or @Priya or @U8")

    def test_a_mention_whose_name_lookup_fails_keeps_its_id(self):
        adapter = _Adapter()
        resolve = adapter._resolve_user_name

        async def flaky(user_id, chat_id="", team_id=""):
            if user_id == "U9":
                raise RuntimeError("user_not_found")
            return await resolve(user_id, chat_id, team_id)

        adapter._resolve_user_name = flaky
        adapter._bot_user_id = "U0BOT"
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": ", <@U0BOT> seeded-b, ask <@U9>"}]
        _run(runtime.settle_question(adapter, SUB))
        note = adapter.updates[0]["blocks"][-1]["elements"][0]["text"]
        self.assertEqual(note, "✓ Priya: seeded-b, ask @U9")

    def test_the_workspaces_own_bot_mention_is_the_one_dropped(self):
        adapter = _Adapter()
        adapter._bot_user_id = "U0OTHER"
        adapter._team_bot_user_ids = {SUB["team_id"]: "U0TEAM"}
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U7", "text": "<@U0TEAM> seeded-b"}]
        _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(adapter.updates[0]["blocks"][-1]["elements"][0]["text"], "✓ Priya: seeded-b")

    def test_a_click_whose_rewrite_failed_settles_without_a_typed_line(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.replies = [{"ts": "1700000000.000400", "user": "U8", "text": "thanks"}]
        clicks = SimpleNamespace(answered=lambda channel, ts: False, clicked=lambda channel, ts: True)
        with mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_clicks=clicks), "gateway.slack_ux_clicks": clicks}):
            _run(runtime.settle_question(adapter, SUB))
        self.assertNotEqual(adapter.updates[0]["blocks"][-1]["type"], "context")
        self.assertEqual(adapter.reads, [])

    def test_a_question_with_no_thread_reads_nothing(self):
        adapter = _Adapter()
        sub = {**SUB, "thread_id": ""}
        _run(runtime.needs_you(adapter, sub, QUESTION, 3))
        _run(runtime.settle_question(adapter, sub))
        self.assertEqual(adapter.reads, [])
        self.assertEqual(len(adapter.updates), 1)

    def test_a_question_a_click_answered_is_left_alone(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        with mock.patch.object(runtime, "_clicked", return_value=True) as clicked:
            _run(runtime.settle_question(adapter, SUB))
        clicked.assert_called_once_with("C0KAGE", POSTED_TS)
        self.assertEqual(adapter.updates, [])

    def test_the_click_record_is_read_from_the_clicks_module(self):
        clicks = SimpleNamespace(answered=lambda channel, ts: (channel, ts) == ("C0KAGE", POSTED_TS))
        with mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_clicks=clicks), "gateway.slack_ux_clicks": clicks}):
            self.assertTrue(runtime._clicked("C0KAGE", POSTED_TS))
            self.assertFalse(runtime._clicked("C0KAGE", "1700000000.000999"))
        with mock.patch.dict(sys.modules, {"gateway": None, "gateway.slack_ux_clicks": None}):
            self.assertFalse(runtime._clicked("C0KAGE", POSTED_TS))

    def test_nothing_posted_nothing_settled(self):
        adapter = _Adapter()
        _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(adapter.updates, [])

    def test_a_failed_rewrite_raises_nothing(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        adapter.fail = True
        with self.assertLogs(runtime.logger, "WARNING"):
            _run(runtime.settle_question(adapter, SUB))

    def test_a_failed_rewrite_keeps_the_question_and_the_next_settle_retries(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        card = runtime.question_card("C0KAGE", POSTED_TS)
        self.assertIsNotNone(card)
        adapter.fail = True
        with self.assertLogs(runtime.logger, "WARNING"):
            _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(runtime.question_card("C0KAGE", POSTED_TS), card)
        adapter.fail = False
        _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(_buttons(adapter.updates[-1]["blocks"]), [])
        self.assertEqual(runtime._questions, {})
        self.assertIsNone(runtime.question_card("C0KAGE", POSTED_TS))

    def test_a_question_asked_again_after_a_failed_settle_is_retried_with_the_next(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        key = runtime._sub_key(SUB)
        first = runtime._questions[key]
        runtime._questions[key] = (first[0], first[1], EARLIER_TS, *first[3:])
        adapter.fail_update = True
        with self.assertLogs(runtime.logger, "WARNING"):
            _run(runtime.settle_question(adapter, SUB))
        self.assertTrue(_run(runtime.needs_you(adapter, SUB, QUESTION, 9)))
        self.assertEqual(runtime._questions[key][0], 9)
        adapter.fail_update = False
        _run(runtime.settle_question(adapter, SUB))
        self.assertEqual([u["ts"] for u in adapter.updates], [EARLIER_TS, POSTED_TS])
        self.assertEqual((runtime._questions, runtime._unsettled), ({}, {}))

    def test_a_click_on_a_question_kept_for_retry_still_names_its_card(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        key = runtime._sub_key(SUB)
        first = runtime._questions[key]
        runtime._questions[key] = (first[0], first[1], EARLIER_TS, *first[3:])
        adapter.fail_update = True
        with self.assertLogs(runtime.logger, "WARNING"):
            _run(runtime.settle_question(adapter, SUB))
        _run(runtime.needs_you(adapter, SUB, QUESTION, 9))
        self.assertEqual(runtime.question_card("C0KAGE", EARLIER_TS), SUB["task_id"])
        self.assertEqual(runtime.question_card("C0KAGE", POSTED_TS), SUB["task_id"])
        self.assertIsNone(runtime.question_card("C0OTHER", EARLIER_TS))

    def test_a_clicked_question_is_forgotten_without_a_rewrite(self):
        adapter = _Adapter()
        _run(runtime.needs_you(adapter, SUB, QUESTION, 3))
        with mock.patch.object(runtime, "_clicked", return_value=True):
            _run(runtime.settle_question(adapter, SUB))
        self.assertEqual(runtime._questions, {})


class _Root:
    """A throwaway Hermes root holding the fixture notifier and the runtime module."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        notifier = self.dir / applier.RELATIVE
        notifier.parent.mkdir(parents=True)
        notifier.write_text(NOTIFIER)
        (self.dir / "gateway" / "__init__.py").write_text("")
        shutil.copy(HERE / "slack_ux_moments.py", self.dir / "gateway")
        shutil.copy(HERE / "kanban_progress_lines.py", self.dir / "gateway")

    def load(self, name):
        """Exec the (possibly patched) fixture notifier with ``gateway`` importable."""
        sys.path.insert(0, str(self.dir))
        try:
            sys.modules.pop("gateway", None)
            sys.modules.pop("gateway.slack_ux_moments", None)
            namespace = {"__name__": name}
            exec(compile((self.dir / applier.RELATIVE).read_text(), name, "exec"), namespace)  # noqa: S102
            return namespace
        finally:
            sys.path.remove(str(self.dir))
            sys.modules.pop("gateway", None)
            sys.modules.pop("gateway.slack_ux_moments", None)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def _wake(namespace, sub=SUB, events=(), wake_kinds=("blocked",)):
    notification = namespace["_KanbanNotification"](sub, list(events), set(wake_kinds))
    notification.build_wake_text()
    return notification.synth


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)

    def test_applies_once(self):
        applier.apply(self.root.dir)
        text = (self.root.dir / applier.RELATIVE).read_text()
        self.assertEqual(text.count(applier.BUILD_MARKER), 1)
        with self.assertRaises(SystemExit):
            applier.apply(self.root.dir)

    def test_drifted_anchor_fails_loudly(self):
        path = self.root.dir / applier.RELATIVE
        drifted = NOTIFIER.replace('t("gateway.kanban.wake.guidance")', 't("gateway.kanban.wake.guide")')
        path.write_text(drifted)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(self.root.dir)
        self.assertIn("wake text guidance line", str(caught.exception))
        self.assertEqual(path.read_text(), drifted)

    def test_the_anchor_is_upstreams_line(self):
        self.assertIn(applier.WAKE_ANCHOR, NOTIFIER)

    def test_flag_off_the_wake_is_upstreams(self):
        upstream = _wake(self.root.load("upstream_notifier"), events=[_event(3)])
        applier.apply(self.root.dir)
        with mock.patch.dict(os.environ, {}, clear=True):
            patched = _wake(self.root.load("patched_notifier"), events=[_event(3)])
        self.assertEqual(patched, upstream)
        self.assertEqual(upstream, WAKE)

    def test_a_posted_question_notes_the_wake(self):
        applier.apply(self.root.dir)
        namespace = self.root.load("patched_notifier_on")
        loaded = namespace["_kage_moments_wake_text"].__globals__
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}):
            _run(loaded["needs_you"](_Adapter(), SUB, QUESTION, 3))
        self.assertEqual(_wake(namespace, events=[_event(3)]), f"{WAKE}\n\n{runtime.WAKE_NOTE} {runtime.WAKE_NOTE_ANSWERED}")
        self.assertEqual(_wake(namespace, events=[_event(3)], wake_kinds=("crashed",)), WAKE)


class VerifierTest(unittest.TestCase):
    def setUp(self):
        runtime._announced.clear()
        root = _Root()
        self.addCleanup(root.cleanup)
        applier.apply(root.dir)
        self.root = root.dir

    def test_passes_on_the_shipped_modules(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            verifier.main(self.root)

    def test_fails_when_the_caller_stops_importing_it(self):
        (self.root / "gateway" / "kanban_progress_lines.py").write_text("")
        with self.assertRaises(SystemExit):
            verifier.main(self.root)

    def test_fails_when_the_wake_is_not_patched(self):
        (self.root / applier.RELATIVE).write_text(NOTIFIER)
        with self.assertRaises(SystemExit):
            verifier.main(self.root)


if __name__ == "__main__":
    unittest.main()
