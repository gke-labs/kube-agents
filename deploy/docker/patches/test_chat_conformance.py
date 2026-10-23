"""Every shared kube-agents.chat/v1 case through the next-mode emitter. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_chat_conformance.py

The cases are ``tests/conformance/chat/`` (``agents/platform/scripts/chat_cases.py``
reads them). A case with one input runs through the composer that lays it out
(``agents/platform/scripts/chat_message.py``, ``slack_ux_incident.chat_triage``, or the
fleet-audit and first-inventory cards, ``slack_audit_report.chat_from_issue`` and
``agents/chat/scripts/inventory_presenter.chat``).
A case that is a sequence of card events runs through the gateway patches that
send it, with a stand-in for the chat.notify adapter recording each send:
``kanban_progress_lines.deliver`` and ``slack_ux_status`` for the plan (the fold
of a fanned-out child included), ``slack_ux_moments`` for a question. The
``reactions-*`` cases pin the ask's mark as a ``notify`` of its own; the
emitter carries it on the plan it sends for the same event (as those cases'
notes say), so the runner reads the ``mark`` off that plan. The entries on the
bridge's own carriers (``progress`` and ``terminal``) are
``a2a/hermes-bridge``'s, which ``chat_test.go`` runs; this file checks only that
the ask kind they carry is the one ``chat_message.ask_kind`` gives.
"""

import asyncio
import html
import itertools
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
#: The Slack renderer's golden files under next, one per shared case it lays out.
SLACK_GOLDEN = HERE.parents[2] / "a2a" / "gateway" / "testdata" / "slack-chat"
CHAT_SCRIPTS = HERE.parents[2] / "agents" / "chat" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(CHAT_SCRIPTS))

import chat_cases
import chat_message
import inventory_presenter
import kanban_notifier
import kanban_progress_lines
import slack_audit_report
import slack_ux_failure
import slack_ux_incident
import slack_ux_moments
import slack_ux_status

NOTIFY = "notify"
RESULT = "result"
BRIDGE_CARRIERS = frozenset({"progress", "terminal"})
CHAT_ID = "a2a-ctx-1"
THREAD = "slack:C1/1.2"
HEADER = "@platform "
#: What a clipped headline ends with.
ELLIPSIS = chat_message._presenter.ELLIPSIS
#: The mrkdwn marker around today's bold headline.
BOLD = "*"


class _ChatAdapter:
    """The home chat.notify stand-in as the patches see it: ``send_chat`` and ``send`` recorded, always accepted."""

    def __init__(self):
        self.sent: list[dict] = []
        self.posts: list[str] = []

    async def send_chat(self, chat_id, content, chat, update="", metadata=None):
        self.sent.append({"chat": chat, "update": update, "text": content, "thread": (metadata or {}).get("thread_id")})
        return SimpleNamespace(success=True, message_id="m", error=None)

    async def send(self, chat_id, content, metadata=None):
        self.posts.append(content)
        return SimpleNamespace(success=True, message_id="m", error=None)

    async def edit_message(self, chat_id, message_id, content):
        return SimpleNamespace(success=False, error="Not supported")


class _ConversationAdapter(_ChatAdapter):
    """The conversation stand-in: it also reads back the kind of ask the route recorded."""

    def __init__(self, ask: str = ""):
        super().__init__()
        self._ask = ask

    def ask(self, chat_id):
        return self._ask


def _sub(card: str, thread: str = THREAD) -> dict:
    return {"task_id": card, "platform": "slack", "chat_id": CHAT_ID, "thread_id": thread, "team_id": ""}


def _reset_status() -> None:
    for store in (slack_ux_status._plans, slack_ux_status._lapsed, slack_ux_status._seen,
                  slack_ux_status._marks_sent, slack_ux_status._titles, slack_ux_status._alert_titles):
        store.clear()


def _row(spec: dict) -> slack_ux_status._Row:
    row = slack_ux_status._Row(spec["task_id"], spec.get("title") or "")
    row.lines = list(spec.get("lines") or ())
    row.steps = int(spec.get("steps") or 0)
    row.note = spec.get("note") or ""
    row.result = spec.get("result") or ""
    row.status = spec["status"]
    return row


def _stem(update: str) -> str:
    """An ``update`` key without its serial: ``plan-t_a-<serial>`` is ``plan-t_a``."""
    return update.rsplit("-", 1)[0]


def _as_case(outs: list, expected: list) -> list:
    """``outs`` with each ``update`` key spelled as the case spells it.

    The emitter ends a key with a serial no other message repeats, before or after a
    restart (``plan-<first card>-<serial>``, ``question-<card>-<serial>``); the cases
    name only the stem (``plan``, ``question-<card>``). A key that is the stem plus a
    ``-`` suffix matches it; any other key is left as it is, and fails the comparison.
    """
    spelled = []
    for out, want in zip(outs, expected):
        stem = (want or {}).get("update", "")
        if out and stem and out.get("update", "").startswith(stem + "-"):
            out = dict(out, update=stem)
        spelled.append(out)
    return spelled + outs[len(expected):]


def _out(chat: dict | None, carrier: str, update: str = "") -> dict | None:
    if chat is None:
        return None
    out = {"carrier": carrier, "chat": chat}
    if update:
        out["update"] = update
    return out


class SharedCasesTest(unittest.TestCase):
    @staticmethod
    def reset() -> None:
        """Empty in-process state, so each case runs as on a thread of its own."""
        for store in (slack_ux_moments._questions, slack_ux_moments._unsettled, slack_ux_moments._announced):
            store.clear()
        _reset_status()

    def setUp(self):
        self.reset()
        # The plan's title never reads an alert's routing row here.
        self.no_alert = mock.patch.object(slack_ux_status, "_alert_title", return_value="")
        self.no_alert.start()
        self.addCleanup(self.no_alert.stop)

    def run_case(self, case: dict) -> list:
        """The outputs the emitter gives for ``case``, one per expected entry, as the case spells them."""
        self.reset()
        given = case["input"]
        if "ledger_issue" in given:
            # The cron relay's fleet-audit card, on the home-channel chat.notify route.
            ref = slack_audit_report.ledger_ref(given["relayed"])
            built = slack_audit_report.chat_from_issue(given["ledger_issue"], ref, given["relayed"])
            return [_out(built and built[0], NOTIFY)]
        if "failed_audit_report" in given:
            built = slack_audit_report.failed_chat(given["failed_audit_report"])
            return [_out(built and built[0], NOTIFY)]
        if "inventory_report" in given:
            built = inventory_presenter.chat(given["inventory_report"])
            return [_out(built and built[0], NOTIFY)]
        if "markdown" in given:
            return [_out(chat_message.answer_message(given["markdown"]), RESULT)]
        if "reason" in given:
            return [_out(chat_message.needs_you_message(given["reason"]), NOTIFY)]
        if "url" in given:
            return [_out(chat_message.pr_message(given["url"], given["repo"], given["number"], given["line"]), NOTIFY)]
        if "reply" in given:
            return [_out(slack_ux_failure.layout(given["reply"], True), RESULT)]
        if "report" in given:
            laid_out = slack_ux_incident.chat_triage(given["report"].strip())
            return [_out(laid_out and laid_out[0], NOTIFY)]
        if "headline" in given:
            fields = {k: given[k] for k in ("note", "rows", "choices", "links", "detail", "after_rows") if k in given}
            return [_out(chat_message.report_message(given["headline"], **fields), RESULT)]
        if "card" in given:
            return self.run_question(given)
        if "rows" in given and "events" in given:
            return self.run_card_events(given)
        if "rows" in given:
            rows = [_row(r) for r in given["rows"]]
            update = chat_message.PLAN_UPDATE.format(card=rows[0].task_id, serial=1)
            return [_out(chat_message.plan_message(given.get("title"), rows), NOTIFY, update)]
        return self.run_marks(given, case["expected"])

    def run_question(self, given: dict) -> list:
        """A card's question and its settle, through ``slack_ux_moments`` on the chat route."""
        adapter = _ChatAdapter()
        sub = {"task_id": given["card"], "platform": "slack", "chat_id": "a2a-ctx-1", "thread_id": "slack:C1/1.2"}
        outs = []
        for i, event in enumerate(given["events"]):
            before = len(adapter.sent)
            if event["kind"] == slack_ux_moments.BLOCKED_KIND:
                asyncio.run(slack_ux_moments.needs_you(adapter, sub, event.get("payload"), i + 1))
            else:
                asyncio.run(slack_ux_moments.settle_question(adapter, sub, event["kind"]))
            sent = adapter.sent[before:]
            outs.append(_out(sent[-1]["chat"], NOTIFY, sent[-1]["update"]) if sent else None)
        return outs

    def deliver(self, adapter, sub: dict, kind: str, payload: dict | None, event_id: int, title: str,
                fanned_out: bool = False) -> None:
        """One notifier event through ``kanban_progress_lines.deliver``, with KAGE_SLACK_UX on for the card."""
        ev = SimpleNamespace(id=event_id, kind=kind, payload=payload or {})
        message = str((payload or {}).get("summary") or (payload or {}).get("note") or kind)

        async def folds(*_args):
            return fanned_out

        async def no_reaction(*_args):
            return None

        with mock.patch.object(kanban_progress_lines, "_slack_quiet", return_value=kanban_notifier), \
                mock.patch.object(kanban_progress_lines, "_slack_plan", return_value=slack_ux_status), \
                mock.patch.object(kanban_progress_lines, "_slack_moments", return_value=None), \
                mock.patch.object(kanban_progress_lines, "_explained_by_wake", return_value=False), \
                mock.patch.object(kanban_progress_lines, "_folds", folds), \
                mock.patch.object(kanban_progress_lines, "_settle_reaction", no_reaction):
            asyncio.run(kanban_progress_lines.deliver(
                SimpleNamespace(), adapter, sub, kind, ev, message, {"thread_id": sub["thread_id"]}, HEADER,
                None, title,
            ))

    def run_card_events(self, given: dict) -> list:
        """The plan each card event leaves, as ``deliver`` and ``slack_ux_status`` send it; a fanned-out
        child's completion folds into its row and posts nothing of its own."""
        adapter = _ChatAdapter()
        titles = {r["task_id"]: r.get("title") or "" for r in given["rows"]}
        event_id = 0
        for row in given["rows"]:
            for line in row.get("lines") or ():
                event_id += 1
                self.deliver(adapter, _sub(row["task_id"]), "heartbeat", {"note": line}, event_id, titles[row["task_id"]])
        outs = []
        for event in given["events"]:
            event_id += 1
            sent, posts = len(adapter.sent), len(adapter.posts)
            self.deliver(adapter, _sub(event["task_id"]), event["kind"], event.get("payload"), event_id,
                         titles[event["task_id"]], bool(event.get("fanned_out")))
            plans = adapter.sent[sent:]
            outs.append(_out(plans[-1]["chat"], NOTIFY, plans[-1]["update"]) if plans else None)
            self.assertEqual(len(adapter.posts) - posts, 0 if event.get("fanned_out") else 1,
                             "a fanned-out child's completion posts nothing; any other posts its report")
        return outs

    def run_marks(self, given: dict, expected: list) -> list:
        """The ask's mark on each card event, read off the plan ``slack_ux_status`` sends for it."""
        outs, ask, adapter, thread = [], "", None, THREAD
        for i, event in enumerate(given["events"]):
            if "ask" in event:
                ask = chat_message.ask_kind(event["ask"])
                adapter, thread = _ConversationAdapter(ask), f"{THREAD}.{i}"
                outs.append(expected[i] if self.bridge_entry(expected[i], ask) else ("ask", ask))
            elif "terminal" in event:
                for card in event.get("filed_cards") or ():
                    asyncio.run(slack_ux_status.start_row(adapter, _sub(card, thread), card))
                outs.append(expected[i] if expected[i] is None or self.bridge_entry(expected[i], ask) else ("terminal", ask))
            else:
                sent = len(adapter.sent)
                asyncio.run(slack_ux_status.settle_row(adapter, _sub(event["task_id"], thread), event["kind"]))
                marks = [s["chat"]["mark"] for s in adapter.sent[sent:] if "mark" in s["chat"]]
                outs.append(_out(chat_message.message(mark=marks[-1]), NOTIFY) if marks else None)
        return outs

    @staticmethod
    def bridge_entry(entry: dict | None, ask: str) -> bool:
        """Whether ``entry`` is the bridge's, carrying ``ask`` (its notify-free half is chat_test.go's)."""
        return (
            entry is not None
            and entry["carrier"] in BRIDGE_CARRIERS
            and entry["chat"].get("mark", {}).get("ask") == ask
        )

    def test_every_case(self):
        cases = chat_cases.load()
        self.assertGreater(len(cases), 0)
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(_as_case(self.run_case(case), case["expected"]), case["expected"])


class QuestionDrawnOnceTest(unittest.TestCase):
    """A question case's headline is not repeated in its detail, under next or today.

    The headline is plain text, so the detail is read as plain too: a code span's
    backticks, a link's url and paired markup do not hide a repeat."""

    def test_every_question_case(self):
        cases = [c for c in chat_cases.load() if "reason" in c["input"]]
        self.assertGreater(len(cases), 0)
        moments = chat_message._moments
        for case in cases:
            reason = case["input"]["reason"]
            with self.subTest(case=case["name"]):
                chat = chat_message.needs_you_message(reason)
                detail = [moments._headline_text(line) for line in chat.get("detail", "").splitlines()]
                shown = chat["headline"].removesuffix(ELLIPSIS)
                self.assertFalse(any(shown in line for line in detail), detail)
                blocks, _text = moments.needs_you(reason)
                title = html.unescape(blocks[0]["text"]["text"]).strip(BOLD).removesuffix(ELLIPSIS)
                _head, rest, _options = moments._question(reason, True)
                below = [moments._headline_text(line) for line in "\n".join(rest).splitlines()]
                self.assertFalse(any(title in line for line in below), below)


class PlanDrawnAlikeTest(unittest.TestCase):
    """Today's plan blocks for a plan case are next's: the Go renderer's golden for the same case."""

    def test_every_plan_case(self):
        cases = [c for c in chat_cases.load() if "rows" in c["input"] and not ({"events", "headline"} & c["input"].keys())]
        self.assertGreater(len(cases), 0)
        for case in cases:
            with self.subTest(case=case["name"]):
                given = case["input"]
                # The window slack_ux_status._prune applies before today draws, as the emitter applies it.
                rows = chat_message._plan_rows([_row(r) for r in given["rows"]])
                today = slack_ux_status._status.plan_blocks(given.get("title"), rows)
                golden = json.loads((SLACK_GOLDEN / f"{case['name']}.json").read_text())
                self.assertEqual(today, golden[0]["blocks"])
                self.assertEqual({b["type"] for b in today}, {"task_card"}, "one line per card, no folded plan")


class ChatRoutePlanTest(unittest.TestCase):
    """``slack_ux_status`` on the chat route: one plan per thread, edited by its key, the mark on change."""

    SUB: ClassVar[dict] = {"task_id": "t_a", "platform": "slack", "chat_id": "a2a-ctx-1", "thread_id": "slack:C1/1.2", "team_id": ""}

    def setUp(self):
        _reset_status()
        slack_ux_status._alert_titles[(CHAT_ID, THREAD)] = ""

    def test_a_card_note_then_its_completion(self):
        adapter = _ConversationAdapter(ask="fleet")

        async def run():
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 1, "seeded-a", "reading version")
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 2, "seeded-a", "found 1.33.4")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "completed", "1.33.4 = default", "seeded-a")

        asyncio.run(run())
        self.assertEqual([_stem(s["update"]) for s in adapter.sent], ["plan-t_a"] * 3)
        self.assertEqual(len({s["update"] for s in adapter.sent}), 1, "one plan, one key")
        self.assertEqual({s["thread"] for s in adapter.sent}, {"slack:C1/1.2"})
        first, second, last = (s["chat"] for s in adapter.sent)
        self.assertEqual(first["mark"], {"ask": "fleet"})
        self.assertNotIn("mark", second, "an unchanged mark is not sent again")
        self.assertEqual(last["mark"], {"settle": "done"})
        self.assertEqual(last["plan"]["items"], [{
            "id": "t_a", "title": "1.33.4 = default", "status": "complete",
            "steps": ["reading version", "found 1.33.4"], "stepCount": 2, "result": "1.33.4 = default",
        }])
        self.assertEqual(adapter.sent[0]["text"], "seeded-a\n◌ reading version")

    def test_the_threads_next_plan_takes_the_next_key(self):
        adapter = _ChatAdapter()

        async def run():
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "completed", "done", "one")
            await slack_ux_status.settle_row(adapter, dict(self.SUB, task_id="t_b"), "completed", "done", "two")

        asyncio.run(run())
        self.assertEqual([_stem(s["update"]) for s in adapter.sent], ["plan-t_a", "plan-t_b"])

    def test_a_plan_after_a_restart_does_not_take_an_earlier_plans_key(self):
        # The notifier restarted between two plans its first card opened in one thread:
        # its memory is gone, and the second plan must still not edit the first's message.
        adapter = _ChatAdapter()
        sub = dict(self.SUB)

        asyncio.run(slack_ux_status.settle_row(adapter, sub, "completed", "done", "one"))
        _reset_status()
        with mock.patch.object(slack_ux_status, "_KEY_NONCE", "restarted"), \
                mock.patch.object(slack_ux_status, "_plan_serials", itertools.count(1)):
            asyncio.run(slack_ux_status.settle_row(adapter, dict(sub), "completed", "done", "one"))
        first, second = (s["update"] for s in adapter.sent)
        self.assertEqual((_stem(first), _stem(second)), ("plan-t_a", "plan-t_a"))
        self.assertNotEqual(first, second)

    def test_a_card_reopening_the_thread_after_a_lapse_posts_a_new_plan(self):
        # The plan lapsed with its first card still running; that card's next note
        # opens a new plan, which must not edit the set-aside one.
        adapter = _ChatAdapter()

        async def run():
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 1, "a", "reading")
            key = (CHAT_ID, THREAD)
            plan = slack_ux_status._plans[key]
            plan.touched = time.monotonic() - slack_ux_status.PLAN_HOLD_SECONDS - 1
            await slack_ux_status._lapse(adapter, key, plan)
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 2, "a", "still reading")

        asyncio.run(run())
        first, second = (s["update"] for s in adapter.sent)
        self.assertEqual(_stem(second), "plan-t_a")
        self.assertNotEqual(first, second)

    def test_the_mark_reads_every_plan_in_the_thread(self):
        # A card that gave up sets its plan aside; the next card runs on plan-2.
        # When the first is retried and completes on its own plan, the thread's
        # ask is still running on plan-2, so no done is sent.
        adapter = _ConversationAdapter(ask="fleet")

        async def run():
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 1, "a", "reading")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "gave_up", "", "a")
            await slack_ux_status.deliver_row(adapter, dict(self.SUB, task_id="t_b"), 2, "b", "reading")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "unblocked", "", "a")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "completed", "fine", "a")
            await slack_ux_status.settle_row(adapter, dict(self.SUB, task_id="t_b"), "completed", "fine", "b")

        asyncio.run(run())
        marks = [(_stem(s["update"]), s["chat"].get("mark")) for s in adapter.sent if "mark" in s["chat"]]
        self.assertEqual(marks, [("plan-t_a", {"ask": "fleet"}), ("plan-t_a", {"settle": "failed"}),
                                 ("plan-t_b", {"ask": "fleet"}), ("plan-t_b", {"settle": "done"})])

    def test_an_earlier_asks_failed_card_does_not_fail_a_later_ask(self):
        # A gave up and was never retried; the next ask's card succeeds, and its
        # ask settles done, as today settles each ask by its own cards.
        adapter = _ConversationAdapter(ask="fleet")

        async def run():
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 1, "a", "reading")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "gave_up", "", "a")
            await slack_ux_status.deliver_row(adapter, dict(self.SUB, task_id="t_b"), 2, "b", "reading")
            await slack_ux_status.settle_row(adapter, dict(self.SUB, task_id="t_b"), "completed", "fine", "b")

        asyncio.run(run())
        marks = [s["chat"]["mark"] for s in adapter.sent if "mark" in s["chat"]]
        self.assertEqual(marks, [{"ask": "fleet"}, {"settle": "failed"}, {"ask": "fleet"}, {"settle": "done"}])

    def test_a_later_asks_settle_goes_even_when_it_equals_the_last_asks(self):
        # Ask 2's card's first event is its completion: its plan opens settled,
        # and its done goes out although ask 1 also ended done.
        adapter = _ConversationAdapter(ask="fleet")

        async def run():
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "completed", "fine", "a")
            await slack_ux_status.settle_row(adapter, dict(self.SUB, task_id="t_b"), "completed", "fine", "b")

        asyncio.run(run())
        marks = [(_stem(s["update"]), s["chat"].get("mark")) for s in adapter.sent]
        self.assertEqual(marks, [("plan-t_a", {"settle": "done"}), ("plan-t_b", {"settle": "done"})])

    def test_with_no_recorded_ask_a_lifted_pause_still_goes(self):
        adapter = _ConversationAdapter(ask="")

        async def run():
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 1, "a", "reading")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "blocked", "", "a")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "unblocked", "", "a")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "completed", "fine", "a")

        asyncio.run(run())
        marks = [s["chat"]["mark"] for s in adapter.sent if "mark" in s["chat"]]
        self.assertEqual(marks, [{"settle": "blocked"}, {}, {"settle": "done"}])

    def test_a_home_thread_carries_no_mark(self):
        adapter = _ChatAdapter()
        asyncio.run(slack_ux_status.settle_row(adapter, dict(self.SUB), "completed", "fine", "a"))
        self.assertNotIn("mark", adapter.sent[-1]["chat"])

    def test_a_refused_post_falls_back_to_progress_lines(self):
        adapter = _ChatAdapter()

        async def refuse(*args, **kwargs):
            return SimpleNamespace(success=False, error="refused")

        adapter.send_chat = refuse
        took = asyncio.run(slack_ux_status.deliver_row(adapter, dict(self.SUB), 1, "seeded-a", "reading"))
        self.assertFalse(took)
        self.assertTrue(slack_ux_status._plans[("a2a-ctx-1", "slack:C1/1.2")].fallback)


if __name__ == "__main__":
    unittest.main()
