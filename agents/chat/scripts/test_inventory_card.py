"""Tests for inventory_card: the first report's card from the stored findings.

``python3 agents/chat/scripts/test_inventory_card.py --write-cases`` rewrites the shared
conformance cases this file pins (``tests/conformance/chat/first-report-*.json``); run
``prettier --write`` on them and ``go test ./gateway -run SlackChatGolden -update-slack-chat``
in ``a2a`` after it.
"""

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "agents" / "platform" / "scripts"))

import bootstrap_delivery
import chat_cases
import chat_message
import findings_queue as fq
import inventory_card
import inventory_findings
import slack_presenter

#: The planted raw files the bootstrap-ranking stack writes: the same findings a live run extracts.
PLANTED = REPO / "bench" / "tf" / "prebuilt" / "bootstrap-ranking"
CRITICALS_RAW = PLANTED / "inventory-raw.txt"
NO_CRITICAL_RAW = PLANTED / "inventory-raw-no-critical.txt"
CASES = REPO / "tests" / "conformance" / "chat"
CASE_PREFIX = "first-report-"
CASE_SOURCE = "agents/chat/scripts/test_inventory_card.py::CasesTest.test_the_shared_cases_are_these_fixtures"
WRITE_CASES_FLAG = "--write-cases"
JSON_INDENT = 2

CRITICAL_RUBRIC = {"B": 8, "L": 10, "detect": 3, "recover": 3, "C": 1.0}
MAJOR_RUBRIC = {"B": 3, "L": 4, "detect": 2, "recover": 2, "C": 1.0}
MINOR_RUBRIC = {"B": 2, "L": 2, "detect": 1, "recover": 2, "C": 0.9}
#: Critical by the floor (L >= 10, B >= 3) at a rank score of 90, behind a major scoring 120.
FLOORED_RUBRIC = {"B": 3, "L": 10, "detect": 1, "recover": 2, "C": 1.0}
MAJOR_HIGH_RUBRIC = {"B": 5, "L": 4, "detect": 3, "recover": 3, "C": 1.0}
LIMIT = fq.DEFAULT_FIRST_REPORT_CRITICALS
#: Enough critical items to pass the chat message's row cap.
MANY = inventory_findings.FIRST_REPORT_ROWS_MAX + 5
#: Each check's words, as the card folds them.
CHECK_WORDS = [words for _, words in inventory_findings.SCAN_CHECKS]
#: A risk longer than the row's budget.
LONG_RISK_WORDS = 120


def _score(rubric: dict, action: str, risk: str, kind: str = "manual") -> dict:
    return {
        "rubric": rubric,
        "recommendation": {"action": action, "rationale": "why", "risk": risk},
        "remediation": {"kind": kind, "note": "how"},
        "verification": {"kind": "manual", "command": "", "still_failing_when": "the object is unchanged"},
    }


def criticals_items() -> list[dict]:
    return inventory_findings.parse_block(CRITICALS_RAW.read_text(encoding="utf-8"))


def criticals_scores() -> dict:
    return {
        "scores": {
            "f001": _score(CRITICAL_RUBRIC, "Move it to Workload Identity.", "Anyone who reads the Secret holds editor on the project."),
            "f002": _score(CRITICAL_RUBRIC, "Point log-shipper at a published tag.", "No node ships its logs."),
            "f003": _score(CRITICAL_RUBRIC, "Raise the 256Mi memory limit or fix the leak.", "The service is down."),
            "f004": _score(CRITICAL_RUBRIC, "Restore the ConfigMap pricing-rules.", "pricing-engine serves nothing."),
            "f005": _score(MAJOR_RUBRIC, "Set readOnlyRootFilesystem: true.", "A compromised container can rewrite its own binaries."),
            "f006": _score(MINOR_RUBRIC, "Add a ResourceQuota and a LimitRange.", "One workload can take the namespace's share."),
            "f007": _score(MINOR_RUBRIC, "Enable Managed Service for Prometheus.", "No workload metrics are collected.", "gcloud"),
        },
        "complete_clusters": ["onboarding-criticals-prod/prod-central"],
    }


def no_critical_items() -> list[dict]:
    return inventory_findings.parse_block(NO_CRITICAL_RAW.read_text(encoding="utf-8"))


def no_critical_scores() -> dict:
    return {
        "scores": {
            "f001": _score(MINOR_RUBRIC, "Set readOnlyRootFilesystem: true.", "A compromised container can rewrite its own binaries."),
            "f002": _score(MINOR_RUBRIC, "Add a startupProbe.", "A slow start reads as a failed liveness check."),
            "f003": _score(MINOR_RUBRIC, "Add a ResourceQuota and a LimitRange.", "One workload can take the namespace's share."),
            "f004": _score(MINOR_RUBRIC, "Enable Managed Service for Prometheus.", "No workload metrics are collected.", "gcloud"),
        },
        "complete_clusters": ["onboarding-posture-prod/prod-east"],
    }


def many_items() -> list[dict]:
    return [
        {
            "id": f"f{n:03d}",
            "check": "crashloop-backoff",
            "project": "wide-prod",
            "cluster": f"cluster-{n:02d}",
            "namespace": "shop",
            "object": f"api-{n:02d}",
            "title": f"api-{n:02d} is in CrashLoopBackOff on cluster-{n:02d}",
        }
        for n in range(1, MANY + 1)
    ]


def many_scores() -> dict:
    return {
        "scores": {f"f{n:03d}": _score(CRITICAL_RUBRIC, "Fix the crash.", "The service is down.") for n in range(1, MANY + 1)},
        "complete_clusters": [f"wide-prod/cluster-{n:02d}" for n in range(1, MANY + 1)],
    }


def shown_for(items: list[dict], scores: dict, limit: int = LIMIT) -> dict:
    """The shown file ``select`` writes for these inputs."""
    chosen, _, _ = inventory_findings.select_items(items, scores["scores"] if items else {}, limit)
    return {
        inventory_findings.SHOWN_ITEMS: [
            {inventory_findings.SHOWN_CLASS: fq.ITEM_CLASSES[0], inventory_findings.SHOWN_IDS: [row["id"] for row in members]}
            for members in chosen
        ]
    }


def limits(**overrides: int) -> fq.PacingLimits:
    return fq.PacingLimits(**overrides)


def build(items: list[dict], scores: dict | None, shown: dict | None = None, pacing: fq.PacingLimits | None = None, **kwargs):
    if shown is None:
        shown = shown_for(items, scores or {"scores": {}}, (pacing or limits()).first_report_criticals)
    return inventory_card.build({"items": items}, scores, pacing or limits(), shown, **kwargs)


def shared_cases() -> dict[str, dict]:
    """The conformance cases this file pins, by name: their inputs and the fixtures behind them."""
    cases = {}
    for name, items, scores, pacing, suppressed in (
        ("two-criticals", criticals_items(), criticals_scores(), limits(), ()),
        ("no-critical-item", no_critical_items(), no_critical_scores(), limits(), ()),
        ("twenty-rows", many_items(), many_scores(), limits(first_report_criticals=MANY), ()),
    ):
        files = {
            "items": {"items": items},
            "scores": scores,
            "limits": {"first_report_criticals": pacing.first_report_criticals},
            "shown": shown_for(items, scores, pacing.first_report_criticals),
        }
        if suppressed:
            files["suppressed"] = list(suppressed)
        card = inventory_card.build(files["items"], scores, pacing, files["shown"], frozenset(suppressed))
        cases[CASE_PREFIX + name] = {
            "name": CASE_PREFIX + name,
            "source": CASE_SOURCE,
            "notes": CASE_NOTES[name],
            "input": {"first_report": files},
            "expected": [{"carrier": "notify", "chat": inventory_card.chat(card)[0]}],
        }
    return cases


CASE_NOTES = {
    "two-criticals": (
        "inventory_card.chat: the first inventory's card from the stage's files. Two of four critical items "
        "are rows with the scored risk and action under them; the queue line counts the rest; the checks fold."
    ),
    "no-critical-item": (
        "inventory_card.chat: a scan with no critical item is done. No rows, no fix button, the queue line "
        "counts the smaller items, and the detail says what the checks found."
    ),
    "twenty-rows": (
        "inventory_card.chat: a first-report limit past the spec's 20 rows. select lists 20, so the shown "
        "file, the rows, the queued count and the See-all count agree."
    ),
}


def write_cases() -> None:
    for name, case in shared_cases().items():
        (CASES / f"{name}.json").write_text(json.dumps(case, indent=JSON_INDENT) + "\n", encoding="utf-8")


class TwoCriticalsTest(unittest.TestCase):
    def setUp(self):
        self.card = build(criticals_items(), criticals_scores())

    def test_the_headline_counts_the_rows(self):
        self.assertEqual(self.card.headline, "First look at your fleet: two things to fix first")

    def test_the_detail_counts_clusters_and_checks(self):
        self.assertEqual(self.card.detail, "1 cluster scanned in full · 9 configuration checks")

    def test_the_rows_are_the_selection_in_order_with_risk_then_action(self):
        self.assertEqual(
            [(row["severity"], row["text"][:14]) for row in self.card.rows],
            [("critical", "invoice-worker"), ("critical", "log-shipper Da")],
        )
        self.assertEqual(
            self.card.rows[0]["detail"],
            "Anyone who reads the Secret holds editor on the project. Move it to Workload Identity.",
        )

    def test_the_queue_line_counts_the_rest_and_the_critical_pace(self):
        self.assertEqual(self.card.after, "5 more items are queued, 2 of them critical. I'll bring up to 2 critical a day.")

    def test_rows_with_only_smaller_items_queued_name_no_pace(self):
        # Both criticals listed and three smaller items queued: the queue adds none of
        # them while a listed critical waits, so the card states no pace for them.
        items = criticals_items()[:2] + criticals_items()[4:]
        scores = criticals_scores()
        scores["scores"] = {item["id"]: scores["scores"][item["id"]] for item in items}
        card = build(items, scores)
        self.assertEqual(card.after, "3 smaller items are queued.")
        self.assertEqual(card.choices, ["Fix the first one", "See all 5 items"])

    def test_the_buttons_and_the_fix_reply(self):
        self.assertEqual(self.card.choices, ["Fix the first one", "See all 7 items"])
        self.assertEqual(self.card.replies, {"Fix the first one": "Fix the first one: " + self.card.rows[0]["text"]})

    def test_the_fold_lists_the_checks(self):
        self.assertEqual(self.card.fold_title, "What the scan checked")
        self.assertEqual([row["text"] for row in self.card.fold_rows], CHECK_WORDS)

    def test_one_critical_item_says_one_thing_and_fix_it(self):
        card = build(criticals_items(), criticals_scores(), pacing=limits(first_report_criticals=1))
        self.assertEqual(card.headline, "First look at your fleet: one thing to fix first")
        self.assertEqual(card.choices, ["Fix it", "See all 7 items"])
        self.assertEqual(card.after, "6 more items are queued, 3 of them critical. I'll bring up to 2 critical a day.")

    def test_a_gathered_item_counts_its_other_objects(self):
        items = criticals_items()
        extra = dict(items[0], id="f008", object="payout-worker", title="payout-worker mounts the same key")
        items.append(extra)
        scores = criticals_scores()
        scores["scores"]["f008"] = scores["scores"]["f001"]
        card = build(items, scores)
        self.assertEqual(card.rows[0]["text"], items[0]["title"] + ", and 1 more object")

    def test_a_gathered_item_names_its_critical_row_not_its_best_ranked_one(self):
        # One check on one cluster: a major scoring 120 ranks ahead of a floored
        # critical scoring 90, so the critical row is not the line's first.
        items = criticals_items()
        extra = dict(items[0], id="f008", object="payout-worker", title="payout-worker mounts the same key")
        items = [items[0], extra] + items[4:]
        scores = criticals_scores()
        scores["scores"]["f001"] = _score(FLOORED_RUBRIC, "Move it to Workload Identity.", "The key is readable.")
        scores["scores"]["f008"] = _score(MAJOR_HIGH_RUBRIC, "Rotate the key.", "The key is old.")
        scores["scores"] = {item["id"]: scores["scores"][item["id"]] for item in items}
        card = build(items, scores)
        self.assertEqual(card.headline, "First look at your fleet: one thing to fix first")
        self.assertEqual(card.rows[0]["text"], items[0]["title"] + ", and 1 more object")
        self.assertEqual(card.rows[0]["detail"], "The key is readable. Move it to Workload Identity.")
        self.assertEqual(card.replies, {"Fix it": "Fix it: " + card.rows[0]["text"]})

    def test_a_long_risk_keeps_the_action_in_the_row(self):
        scores = criticals_scores()
        scores["scores"]["f001"]["recommendation"]["risk"] = " ".join(["risk"] * LONG_RISK_WORDS)
        card = build(criticals_items(), scores)
        detail = card.rows[0]["detail"]
        self.assertLessEqual(len(detail), slack_presenter.ROW_TEXT_MAX)
        self.assertTrue(detail.endswith("Move it to Workload Identity."), detail)

    def test_a_long_title_keeps_the_reply_within_the_spec(self):
        items = criticals_items()
        items[0]["title"] = " ".join(["word"] * LONG_RISK_WORDS)
        card = build(items, criticals_scores())
        reply = card.replies["Fix the first one"]
        self.assertLessEqual(len(reply), chat_message.REPLY_MAX)
        self.assertTrue(reply.startswith("Fix the first one: word word"))
        self.assertEqual(inventory_card.chat(card)[0]["choices"][0]["reply"], reply)

    def test_a_zero_daily_limit_says_the_critical_items_wait(self):
        card = build(criticals_items(), criticals_scores(), pacing=limits(daily_criticals=0))
        self.assertEqual(card.after, "5 more items are queued, 2 of them critical. The critical ones wait until you ask.")

    def test_the_others_limit_does_not_reach_the_card(self):
        for noncritical_max in (0, 3):
            with self.subTest(noncritical_max=noncritical_max):
                card = build(criticals_items(), criticals_scores(), pacing=limits(noncritical_max=noncritical_max))
                self.assertEqual(card.after, self.card.after)

    def test_only_critical_items_queued(self):
        items = criticals_items()[:4]
        scores = criticals_scores()
        scores["scores"] = {item["id"]: scores["scores"][item["id"]] for item in items}
        card = build(items, scores)
        self.assertEqual(card.after, "2 more items are queued, 2 of them critical. I'll bring up to 2 critical a day.")


class DoneTest(unittest.TestCase):
    def test_a_clean_fleet_has_no_scores_file_and_is_done(self):
        card = inventory_card.build({"items": []}, None, limits(), {"items": []})
        self.assertEqual(card.headline, "First look at your fleet: done")
        self.assertEqual(card.detail, "no critical item in this scan's 9 checks")
        self.assertEqual((card.rows, card.after, card.choices, card.replies), ([], "", [], {}))
        self.assertEqual([row["text"] for row in card.fold_rows], CHECK_WORDS)

    def test_no_critical_item_counts_the_smaller_ones(self):
        card = build(no_critical_items(), no_critical_scores())
        self.assertEqual(card.headline, "First look at your fleet: done")
        self.assertEqual(card.detail, "1 cluster scanned in full · no critical item in this scan's 9 checks")
        self.assertEqual(card.after, "4 smaller items are queued.")
        self.assertEqual(card.choices, ["See all 4 items"])
        self.assertEqual(build(no_critical_items(), no_critical_scores(), pacing=limits(noncritical_max=0)).after, card.after)

    def test_one_smaller_item_is_singular(self):
        items = no_critical_items()[:1]
        scores = no_critical_scores()
        scores["scores"] = {items[0]["id"]: scores["scores"][items[0]["id"]]}
        card = build(items, scores)
        self.assertEqual(card.after, "1 smaller item is queued.")
        self.assertEqual(card.choices, ["See all 1 item"])


class DismissedTest(unittest.TestCase):
    def test_a_dismissed_row_leaves_the_rows_but_not_the_check(self):
        items, scores = criticals_items(), criticals_scores()
        shown = shown_for(items, scores)
        dismissed = shown["items"][0]["ids"][0]
        card = build(items, scores, shown, suppressed=frozenset({dismissed}))
        self.assertEqual(card.headline, "First look at your fleet: one thing to fix first")
        self.assertEqual([row["text"][:11] for row in card.rows], ["log-shipper"])
        self.assertEqual(card.choices, ["Fix it", "See all 6 items"])

    def test_a_dismissed_row_is_not_counted_in_the_queue(self):
        items, scores = criticals_items(), criticals_scores()
        shown = shown_for(items, scores)
        dismissed = shown["items"][0]["ids"][0]
        card = build(items, scores, shown, suppressed=frozenset({dismissed}))
        # Four criticals less the dismissed one less the one row: two wait, with the three others.
        self.assertEqual(card.after, "5 more items are queued, 2 of them critical. I'll bring up to 2 critical a day.")

    def test_every_listed_row_dismissed_leaves_the_criticals_queued_not_done(self):
        items, scores = criticals_items(), criticals_scores()
        shown = shown_for(items, scores)
        dismissed = frozenset(fid for entry in shown["items"] for fid in entry["ids"])
        card = build(items, scores, shown, suppressed=dismissed)
        self.assertEqual(card.headline, "First look at your fleet: 2 critical items queued")
        self.assertEqual(card.detail, "1 cluster scanned in full · 9 configuration checks")
        self.assertEqual(card.after, "5 more items are queued, 2 of them critical. I'll bring up to 2 critical a day.")
        self.assertEqual((card.rows, card.choices), ([], ["See all 5 items"]))

    def test_a_shown_file_select_wrote_with_exclusions_matches(self):
        # The SOP's path: register reached the queue, so select ran with --exclude for the dismissed id
        # and the next critical took its place in the shown file.
        items, scores = criticals_items(), criticals_scores()
        plain = shown_for(items, scores)
        dismissed = plain["items"][0]["ids"][0]
        chosen, _, _ = inventory_findings.select_items(items, scores["scores"], LIMIT, frozenset({dismissed}))
        shown = {"items": [{"class": "critical", "ids": [row["id"] for row in members]} for members in chosen]}
        self.assertNotEqual(shown, plain)
        card = build(items, scores, shown, suppressed=frozenset({dismissed}))
        self.assertEqual([row["text"][:11] for row in card.rows], ["log-shipper", "pricing-eng"])
        self.assertEqual(card.headline, "First look at your fleet: two things to fix first")
        self.assertEqual(card.after, "4 more items are queued, 1 of them critical. I'll bring up to 2 critical a day.")
        self.assertEqual(card.choices, ["Fix the first one", "See all 6 items"])

    def test_a_dismissed_critical_row_takes_its_gathered_line_off_the_card(self):
        # The sandboxed select wrote the plain recompute, and the user had dismissed the
        # line's only critical row: what is left is a major, so it is no row, and it counts
        # among the others, not the queued criticals.
        items = criticals_items()
        extra = dict(items[0], id="f008", object="payout-worker", title="payout-worker mounts the same key")
        items.append(extra)
        scores = criticals_scores()
        scores["scores"]["f008"] = _score(MAJOR_RUBRIC, "Rotate the key.", "The key is old.")
        shown = shown_for(items, scores)
        dismissed, _ = shown["items"][0]["ids"]
        self.assertTrue(dismissed.endswith(items[0]["object"]), dismissed)
        card = build(items, scores, shown, suppressed=frozenset({dismissed}))
        self.assertEqual(card.headline, "First look at your fleet: one thing to fix first")
        self.assertEqual([(row["severity"], row["text"][:11]) for row in card.rows], [("critical", "log-shipper")])
        self.assertEqual(card.after, "6 more items are queued, 2 of them critical. I'll bring up to 2 critical a day.")
        self.assertEqual(card.choices, ["Fix it", "See all 7 items"])

    def test_a_dismissed_id_with_a_shown_file_matching_neither_recompute(self):
        items, scores = criticals_items(), criticals_scores()
        shown = shown_for(items, scores)
        shown["items"].reverse()
        with self.assertRaises(inventory_card.CardUnavailable):
            build(items, scores, shown, suppressed=frozenset({shown["items"][0]["ids"][0]}))


class ListTurnedOffTest(unittest.TestCase):
    def test_a_zero_first_report_limit_queues_every_critical_and_is_not_done(self):
        card = build(criticals_items(), criticals_scores(), pacing=limits(first_report_criticals=0))
        self.assertEqual(card.headline, "First look at your fleet: 4 critical items queued")
        self.assertEqual(card.detail, "1 cluster scanned in full · 9 configuration checks")
        self.assertEqual(card.after, "7 more items are queued, 4 of them critical. I'll bring up to 2 critical a day.")
        self.assertEqual((card.rows, card.choices), ([], ["See all 7 items"]))


class RowCapTest(unittest.TestCase):
    def test_a_limit_past_the_row_cap_lists_the_cap_and_queues_the_rest(self):
        # select lists at most the rows one card carries, so the shown file, the
        # marks and the card name the same items and the rest stay new in the queue.
        cap = inventory_findings.FIRST_REPORT_ROWS_MAX
        self.assertLessEqual(cap, chat_message.ROWS_MAX)
        shown = shown_for(many_items(), many_scores(), MANY)
        self.assertEqual(len(shown["items"]), cap)
        card = build(many_items(), many_scores(), shown, pacing=limits(first_report_criticals=MANY))
        self.assertEqual(len(card.rows), cap)
        self.assertEqual(card.headline, f"First look at your fleet: {cap} things to fix first")
        self.assertEqual(card.after, "5 more items are queued, 5 of them critical. I'll bring up to 2 critical a day.")
        self.assertEqual(card.choices, ["Fix the first one", f"See all {MANY} items"])
        message = inventory_card.chat(card)[0]
        self.assertEqual(len(message["rows"]), cap)


class UnavailableTest(unittest.TestCase):
    def assertUnavailable(self, text: str, *args, **kwargs):
        with self.assertRaises(inventory_card.CardUnavailable) as caught:
            inventory_card.build(*args, **kwargs)
        self.assertIn(text, str(caught.exception))

    def test_items_without_a_scores_file(self):
        items = criticals_items()
        self.assertUnavailable("no scores file", {"items": items}, None, limits(), shown_for(items, criticals_scores()))

    def test_a_file_that_is_not_an_object(self):
        self.assertUnavailable("the items file is not a JSON object", [], None, limits(), {"items": []})
        self.assertUnavailable("the shown file is not a JSON object", {"items": []}, None, limits(), [])
        self.assertUnavailable("the scores file is not a JSON object", {"items": criticals_items()}, [], limits(), {"items": []})

    def test_a_file_without_its_list(self):
        self.assertUnavailable("no `items` list", {"total": 0}, None, limits(), {"items": []})
        self.assertUnavailable("no `items` list", {"items": []}, None, limits(), {})
        self.assertUnavailable("no `scores` map", {"items": criticals_items()}, {"complete_clusters": []}, limits(), {"items": []})

    def test_a_shown_item_without_its_ids_list(self):
        for entry in ({"class": "critical"}, {"class": "critical", "ids": "f001"}, "f001"):
            with self.subTest(entry=entry):
                self.assertUnavailable("an item in the shown file has no `ids` list", {"items": []}, None, limits(), {"items": [entry]})

    def test_complete_clusters_that_is_not_a_list_of_names(self):
        items = criticals_items()
        for value in ("onboarding-criticals-prod/prod-central", 1, ["onboarding-criticals-prod/prod-central", 1]):
            with self.subTest(value=value):
                scores = dict(criticals_scores(), complete_clusters=value)
                self.assertUnavailable("`complete_clusters`", {"items": items}, scores, limits(), shown_for(items, scores))

    def test_an_unscored_item_cannot_be_selected(self):
        scores = criticals_scores()
        del scores["scores"]["f003"]
        self.assertUnavailable("unscored: f003", {"items": criticals_items()}, scores, limits(), {"items": []})

    def test_a_shown_file_that_differs_from_the_recompute(self):
        items, scores = criticals_items(), criticals_scores()
        shown = shown_for(items, scores)
        shown["items"].reverse()
        self.assertUnavailable("differ from the shown file", {"items": items}, scores, limits(), shown)
        self.assertUnavailable("differ from the shown file", {"items": items}, scores, limits(), {"items": []})


class LayoutsTest(unittest.TestCase):
    def setUp(self):
        self.card = build(criticals_items(), criticals_scores())

    def test_the_chat_message_is_a_valid_report(self):
        message, text = inventory_card.chat(self.card)
        chat_cases.check_chat(message, "chat")
        self.assertEqual(message["headline"], self.card.headline)
        self.assertEqual(message["detail"], self.card.detail)
        self.assertEqual(message["after"], [self.card.after])
        self.assertEqual(message["fold"], {"title": "What the scan checked", "text": "\n".join(CHECK_WORDS)})
        self.assertEqual(
            message["choices"],
            [
                {"text": "Fix the first one", "reply": self.card.replies["Fix the first one"], "primary": True},
                {"text": "See all 7 items"},
            ],
        )
        self.assertTrue(text.startswith("**First look at your fleet: two things to fix first**\n1 cluster scanned"))
        self.assertIn("`critical` invoice-worker", text)
        self.assertTrue(text.rstrip().endswith(self.card.after))

    def test_the_blocks_carry_the_rows_the_fold_and_the_buttons(self):
        blocks, text = inventory_card.blocks(self.card)
        types = [block["type"] for block in blocks]
        self.assertEqual(types.count("divider"), 2)
        fold = next(block for block in blocks if block.get("is_collapsible"))
        self.assertEqual(fold["title"]["text"], "What the scan checked")
        actions = next(block for block in blocks if block["type"] == "actions")
        self.assertEqual([button["text"]["text"] for button in actions["elements"]], ["Fix the first one", "See all 7 items"])
        self.assertEqual(actions["elements"][0]["action_id"], "kage_inventory.choice.0")
        self.assertEqual(actions["elements"][0]["style"], "primary")
        self.assertTrue(text.startswith("*First look at your fleet: two things to fix first 1 cluster scanned in full"))

    def test_the_done_card_lays_out_without_rows_or_buttons(self):
        card = inventory_card.build({"items": []}, None, limits(), {"items": []})
        message = inventory_card.chat(card)[0]
        chat_cases.check_chat(message, "chat")
        self.assertNotIn("rows", message)
        self.assertNotIn("choices", message)
        self.assertNotIn("after", message)
        blocks = inventory_card.blocks(card)[0]
        self.assertNotIn("actions", [block["type"] for block in blocks])


class CasesTest(unittest.TestCase):
    def test_the_shared_cases_are_these_fixtures(self):
        expected = shared_cases()
        on_disk = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in CASES.glob(f"{CASE_PREFIX}*.json")}
        self.assertEqual(sorted(on_disk), sorted(expected))
        for name, case in expected.items():
            with self.subTest(case=name):
                self.assertEqual(on_disk[name], case)

    def test_the_file_keys_match_delivery(self):
        self.assertEqual(inventory_card.ITEMS_KEY, bootstrap_delivery.ITEMS_KEY)
        self.assertEqual(inventory_card.SCORES_KEY, bootstrap_delivery.SCORES_KEY)


if __name__ == "__main__":
    if WRITE_CASES_FLAG in sys.argv:
        write_cases()
    else:
        unittest.main()
