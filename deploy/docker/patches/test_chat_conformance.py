"""Every shared kube-agents.chat/v1 case through the next-mode emitter. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_chat_conformance.py

The cases are ``tests/conformance/chat/`` (``agents/platform/scripts/chat_cases.py``
reads them). Each runs through the code that sends it under ``spec.mode:
next``: the pure composers in ``agents/platform/scripts/chat_message.py``, and,
where a case is a sequence of card events, the gateway patches that drive
them, with a stand-in for the chat.notify adapter that records what it is
handed. The entries a case expects on the bridge's own carriers (``progress``
and ``terminal``) are ``a2a/hermes-bridge``'s, and ``chat_test.go`` runs them
there; this file checks only that the ask kind they carry is the one
``chat_message.ask_kind`` gives.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import chat_cases
import chat_message
import kanban_progress_lines
import slack_status
import slack_ux_failure
import slack_ux_incident
import slack_ux_moments
import slack_ux_status

NOTIFY = "notify"
RESULT = "result"
BRIDGE_CARRIERS = frozenset({"progress", "terminal"})


class _ChatAdapter:
    """The chat.notify stand-in as the patches see it: ``send_chat`` recorded, always accepted."""

    def __init__(self, ask: str = ""):
        self.sent: list[dict] = []
        self._ask = ask

    async def send_chat(self, chat_id, content, chat, update="", metadata=None):
        self.sent.append({"chat": chat, "update": update, "text": content, "thread": (metadata or {}).get("thread_id")})
        return SimpleNamespace(success=True, message_id="m", error=None)

    def ask(self, chat_id):
        return self._ask


def _row(spec: dict) -> slack_ux_status._Row:
    row = slack_ux_status._Row(spec["task_id"], spec.get("title") or "")
    row.lines = list(spec.get("lines") or ())
    row.steps = int(spec.get("steps") or 0)
    row.note = spec.get("note") or ""
    row.result = spec.get("result") or ""
    row.status = spec["status"]
    return row


def _out(chat: dict | None, carrier: str, update: str = "") -> dict | None:
    if chat is None:
        return None
    out = {"carrier": carrier, "chat": chat}
    if update:
        out["update"] = update
    return out


class SharedCasesTest(unittest.TestCase):
    def setUp(self):
        for store in (slack_ux_moments._questions, slack_ux_moments._unsettled, slack_ux_moments._announced):
            store.clear()

    def run_case(self, case: dict) -> list:
        """The outputs the emitter gives for ``case``, one per expected entry, as the case spells them."""
        given = case["input"]
        if "markdown" in given:
            return [_out(chat_message.answer_message(given["markdown"]), RESULT)]
        if "reason" in given:
            return [_out(chat_message.needs_you_message(given["reason"]), NOTIFY)]
        if "url" in given:
            return [_out(chat_message.pr_message(given["url"], given["repo"], given["number"], given["line"]), NOTIFY)]
        if "reply" in given:
            _bolded, offer = slack_ux_failure.present(given["reply"])
            return [_out(chat_message.failure_message(given["reply"], bool(offer)), RESULT)]
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
            return [_out(chat_message.plan_message(given.get("title"), rows), NOTIFY, chat_message.PLAN_UPDATE)]
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

    def run_card_events(self, given: dict) -> list:
        """A plan's rows moved by card events as ``slack_ux_status`` moves them, the plan after each."""
        rows = {r["task_id"]: _row(r) for r in given["rows"]}
        outs = []
        for event in given["events"]:
            result = kanban_progress_lines.result_line(event["kind"], event.get("payload"))
            slack_ux_status._move(rows[event["task_id"]], event["kind"], result)
            outs.append(_out(chat_message.plan_message(None, list(rows.values())), NOTIFY, chat_message.PLAN_UPDATE))
        return outs

    def run_marks(self, given: dict, expected: list) -> list:
        """The ask's reaction state on a card's notifies: :func:`chat_message.plan_mark` as cards move, sent on change."""
        outs, rows, ask, sent = [], {}, "", None
        for i, event in enumerate(given["events"]):
            if "ask" in event:
                ask, rows, sent = chat_message.ask_kind(event["ask"]), {}, None
                outs.append(expected[i] if self.bridge_entry(expected[i], ask) else ("ask", ask))
            elif "terminal" in event:
                for card in event.get("filed_cards") or ():
                    row = rows[card] = slack_ux_status._Row(card, card)
                    row.status = slack_status.TASK_RUNNING
                outs.append(expected[i] if expected[i] is None or self.bridge_entry(expected[i], ask) else ("terminal", ask))
            else:
                row = rows[event["task_id"]]
                if not slack_ux_status._move(row, event["kind"]):
                    outs.append(None)
                    continue
                mark = chat_message.plan_mark(ask, list(rows.values()))
                outs.append(None if mark == sent else _out(chat_message.message(mark=mark), NOTIFY))
                sent = mark
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
                self.assertEqual(self.run_case(case), case["expected"])


class ChatRoutePlanTest(unittest.TestCase):
    """``slack_ux_status`` on the chat route: one plan per thread, edited by its key, the mark on change."""

    SUB: ClassVar[dict] = {"task_id": "t_a", "platform": "slack", "chat_id": "a2a-ctx-1", "thread_id": "slack:C1/1.2", "team_id": ""}

    def setUp(self):
        for store in (slack_ux_status._plans, slack_ux_status._lapsed, slack_ux_status._seen,
                      slack_ux_status._plan_keys, slack_ux_status._titles, slack_ux_status._alert_titles):
            store.clear()
        slack_ux_status._alert_titles[("a2a-ctx-1", "slack:C1/1.2")] = ""

    def test_a_card_note_then_its_completion(self):
        adapter = _ChatAdapter(ask="fleet")

        async def run():
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 1, "seeded-a", "reading version")
            await slack_ux_status.deliver_row(adapter, dict(self.SUB), 2, "seeded-a", "found 1.33.4")
            await slack_ux_status.settle_row(adapter, dict(self.SUB), "completed", "1.33.4 = default", "seeded-a")

        asyncio.run(run())
        self.assertEqual([s["update"] for s in adapter.sent], ["plan", "plan", "plan"])
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
        self.assertEqual([s["update"] for s in adapter.sent], ["plan", "plan-2"])

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
