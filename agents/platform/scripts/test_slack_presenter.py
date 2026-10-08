"""Unit tests for slack_presenter — answer layout, buttons and reactions.

Run: python3 -m pytest agents/platform/scripts/test_slack_presenter.py
"""

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_presenter as sp

#: Slack's cap on a message's text, and how long one ask that size may take to read: it is
#: read on the gateway's event loop, so every thread waits on it.
SLACK_MESSAGE_MAX = 40_000
ARRIVAL_BUDGET_SECONDS = 0.1
#: A run of this many backticks took 7 seconds while a code span's closing run was
#: searched for once per possible opening length.
BACKTICK_REPEATS = 20_000
#: 40 KB of an unclosed ``**`` opener (Slack's message ceiling) took 3 seconds while a bold
#: body could run past later markers; ``[a`` repeated took as long through the link pattern.
MARKUP_REPEATS = 10_000


class FlagTest(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sp.enabled())

    def test_on_values(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": value}):
                self.assertTrue(sp.enabled(), value)

    def test_other_values_are_off(self):
        for value in ("", "0", "false", "off", "no", "enabled"):
            with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": value}):
                self.assertFalse(sp.enabled(), value)


class ArrivalReactionTest(unittest.TestCase):
    CASES = (
        # investigate -> mag
        ("is checkout-gateway restarting? it looks like it's crashlooping?", "mag"),
        ("why is the node pool at 90%?", "mag"),
        ("what are these 502 errors on checkout?", "mag"),
        ("is checkout crashlooping?", "mag"),
        ("diagnose the pending pods on seeded-a", "mag"),
        # fleet -> globe_with_meridians
        ("which clusters have pods restarting right now?", "globe_with_meridians"),
        ("check all clusters for unready nodes", "globe_with_meridians"),
        ("is ingress healthy across the fleet?", "globe_with_meridians"),
        ("<@U0KAGE> list each of our clusters", "globe_with_meridians"),
        # upgrade or version -> arrow_up
        ("<@U0KAGE> what version is seeded-b on", "arrow_up"),
        ("upgrade seeded-c to 1.31", "arrow_up"),
        ("which clusters are behind their release channel default?", "arrow_up"),
        ("is anything out of date?", "arrow_up"),
        # cost -> moneybag
        ("what did seeded-a cost last month?", "moneybag"),
        ("where is our GKE spend going?", "moneybag"),
        ("are we over budget on all clusters?", "moneybag"),
        # security -> shield
        ("run a security audit on seeded-b", "shield"),
        ("any CVEs in the checkout image?", "shield"),
        ("who has cluster-admin? check the RBAC", "shield"),
        ("is the dashboard exposed to the internet?", "shield"),
        # nothing more specific -> eyes
        ("check the ingress certs on seeded-a", "eyes"),
        ("how many nodes does seeded-a have?", "eyes"),
        # change -> hammer_and_wrench
        ("fix it", "hammer_and_wrench"),
        ("can you fix the crashlooping pod?", "hammer_and_wrench"),
        ("bump the image to 1.4.2", "hammer_and_wrench"),
        ("roll back checkout to yesterday's release", "hammer_and_wrench"),
        ("please rollback the deploy", "hammer_and_wrench"),
        ("open a PR for the memory limit", "hammer_and_wrench"),
        ("open a pull request with that change", "hammer_and_wrench"),
        ("scale the web deployment down to 2", "hammer_and_wrench"),
        ("fix the version skew on seeded-c", "hammer_and_wrench"),
        # board -> clipboard
        ("board", "clipboard"),
        ("what's running right now?", "clipboard"),
        ("status of the fleet audit", "clipboard"),
        # incident -> rotating_light
        ("checkout is down", "rotating_light"),
        ("we're paging on payments", "rotating_light"),
        ("sev1 outage in us-east4", "rotating_light"),
        ("payments pods in crashloopbackoff", "rotating_light"),
        # no match -> eyes
        ("thanks", "eyes"),
        ("", "eyes"),
        (None, "eyes"),
    )

    def test_keyword_map(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(sp.arrival_reaction(text), expected)

    def test_word_boundaries(self):
        # "prefix", "downstream", "statusline" and "scaled-up names" are not the keywords.
        self.assertEqual(sp.arrival_reaction("the prefix looks odd"), "eyes")
        self.assertEqual(sp.arrival_reaction("downstream latency"), "eyes")
        self.assertEqual(sp.arrival_reaction("the statusline plugin"), "eyes")
        self.assertEqual(sp.arrival_reaction("the costume party"), "eyes")
        self.assertEqual(sp.arrival_reaction("an oomph of bloom"), "eyes")
        self.assertEqual(sp.arrival_reaction("the clusterrole binding"), "eyes")

    def test_change_beats_incident_and_question(self):
        self.assertEqual(sp.arrival_reaction("prod is down, roll back now"), "hammer_and_wrench")
        self.assertEqual(sp.arrival_reaction("should we scale down?"), "hammer_and_wrench")

    def test_a_run_of_unclosed_mentions_is_read_fast(self):
        # A mention match scanning to the end from every "<" took 0.37 s on these.
        for unit in ("<!", "<@", "<#"):
            with self.subTest(unit=unit):
                start = time.monotonic()
                self.assertEqual(sp.arrival_reaction(unit * (SLACK_MESSAGE_MAX // len(unit))), "eyes")
                self.assertLess(time.monotonic() - start, ARRIVAL_BUDGET_SECONDS)


class SettleReactionTest(unittest.TestCase):
    def test_outcomes(self):
        # Done leaves no reaction: the answer in the thread says it.
        self.assertIsNone(sp.settle_reaction("done"))
        self.assertEqual(sp.settle_reaction("blocked"), "double_vertical_bar")
        self.assertEqual(sp.settle_reaction("failed"), "x")
        self.assertIsNone(sp.settle_reaction("cancelled"))

    def test_kanban_kinds(self):
        self.assertEqual(sp.settle_for_kanban_kind("completed"), "done")
        self.assertEqual(sp.settle_for_kanban_kind("blocked"), "blocked")
        self.assertEqual(sp.settle_for_kanban_kind("review_requested"), "blocked")
        self.assertEqual(sp.settle_for_kanban_kind("gave_up"), "failed")
        self.assertEqual(sp.settle_for_kanban_kind("block_loop_detected"), "blocked")
        # Retried by the dispatcher, or bookkeeping: nothing has settled.
        for kind in ("crashed", "timed_out", "status", "heartbeat", "archived", "unblocked"):
            self.assertIsNone(sp.settle_for_kanban_kind(kind), kind)

    def test_every_reaction_is_distinct(self):
        names = set(sp.SETTLE_REACTIONS.values()) | {
            sp.REACTION_QUESTION, sp.REACTION_INVESTIGATE, sp.REACTION_FLEET, sp.REACTION_UPGRADE,
            sp.REACTION_COST, sp.REACTION_SECURITY, sp.REACTION_CHANGE, sp.REACTION_BOARD,
            sp.REACTION_INCIDENT,
        }
        self.assertEqual(len(names), 11)


class SplitAnswerTest(unittest.TestCase):
    def test_first_sentence_is_headline(self):
        md = "checkout-gateway is crashlooping on an OOM. It hit its 256Mi limit.\n\nRaise it to 512Mi."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "checkout-gateway is crashlooping on an OOM.")
        self.assertEqual(body, ["It hit its 256Mi limit.", "Raise it to 512Mi."])

    def test_markdown_stripped_from_headline(self):
        headline, body = sp.split_answer("## **Yes**, [seeded-a](https://x) is `healthy`\n- one\n- two")
        self.assertEqual(headline, "Yes, seeded-a is healthy")
        self.assertEqual(body, ["- one\n- two"])

    def test_split_lead_keeps_the_sentence_as_written(self):
        sentence = "The [pool](https://x) is at `4/4` nodes" + " and busy" * 20 + "."
        md = sentence + " It needs more.\n\nScale it."
        lead, body = sp.split_lead(md)
        self.assertEqual((lead, body), (sentence, ["It needs more.", "Scale it."]))
        self.assertEqual((sp._clip(sp._plain(lead), sp.HEADLINE_MAX), body), sp.split_answer(md))

    def test_empty(self):
        self.assertEqual(sp.split_answer(""), ("", []))
        self.assertEqual(sp.split_answer("   \n\n "), ("", []))

    def test_code_fence_not_split(self):
        md = "Here it is.\n\n```\na\n\nb\n```\n\nDone."
        _, body = sp.split_answer(md)
        self.assertEqual(body, ["```\na\n\nb\n```", "Done."])

    def test_leading_code_block_has_no_headline(self):
        headline, body = sp.split_answer("```\nkubectl get pods\n```")
        self.assertEqual(headline, "")
        self.assertEqual(body, ["```\nkubectl get pods\n```"])

    def test_an_abbreviation_does_not_end_the_headline(self):
        for line in (
            "Pods fail readiness, e.g. checkout-gateway in ns. prod.",
            "Pods fail readiness, e.g. Checkout in prod.",
        ):
            self.assertEqual(sp.split_answer(line), (line, []))

    def test_a_headline_is_not_cut_after_etc(self):
        headline, _body = sp.split_answer("Deployments, StatefulSets, etc. All fail readiness. Node-a is cordoned.")
        self.assertEqual(headline, "Deployments, StatefulSets, etc. All fail readiness.")

    def test_a_soft_wrapped_first_sentence_is_the_whole_headline(self):
        md = "payments-api keeps crashing: secret\npayments-db-creds is missing. Since 14:02.\n- one"
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api keeps crashing: secret payments-db-creds is missing.")
        self.assertEqual(body, ["Since 14:02.\n- one"])

    def test_a_fence_under_the_first_line_is_not_joined_into_the_headline(self):
        md = "payments-api is crashlooping with:\n```\nOOMKilled. Exit 137\n```\nSince 14:02."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api is crashlooping with:")
        self.assertEqual(body, ["```\nOOMKilled. Exit 137\n```\nSince 14:02."])

    def test_a_leading_code_span_is_not_a_fence(self):
        md = "```payments-api``` is crashlooping on OOM.\n\nRaise the limit."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api is crashlooping on OOM.")
        self.assertEqual(body, ["Raise the limit."])

    def test_a_period_inside_a_code_span_does_not_end_the_headline(self):
        headline, body = sp.split_answer("The pod logs `connection refused. Retrying` on every start. Since 14:02.")
        self.assertEqual(headline, "The pod logs connection refused. Retrying on every start.")
        self.assertEqual(body, ["Since 14:02."])

    def test_fallback_text_keeps_a_plain_headline_as_given(self):
        headline, _body = sp.split_answer("`__init__.py` is missing.")
        self.assertEqual(sp.fallback_text(headline), "*__init__.py is missing.*")

    def test_a_leading_heading_is_not_joined_to_the_next_line(self):
        headline, body = sp.split_answer("##\tSummary\nCheckout is down. Since 14:02.")
        self.assertEqual(headline, "Summary")
        self.assertEqual(body, ["Checkout is down. Since 14:02."])

    def test_bold_around_the_first_sentence_leaves_no_stray_markers(self):
        headline, body = sp.split_answer("**Checkout is crashlooping on OOM. Raise the limit.**")
        self.assertEqual(headline, "Checkout is crashlooping on OOM.")
        self.assertEqual(body, ["**Raise the limit.**"])
        headline, body = sp.split_answer("**Checkout is down.** Raise the limit.")
        self.assertEqual(headline, "Checkout is down.")
        self.assertEqual(body, ["Raise the limit."])
        self.assertEqual(sp.fallback_text(headline), "*Checkout is down.*")

    def test_italic_around_the_first_sentence_leaves_no_stray_markers(self):
        for marker in ("*", "_"):
            headline, body = sp.split_answer(f"{marker}It is down. Restart it.{marker}")
            self.assertEqual(headline, "It is down.")
            self.assertEqual(body, [f"{marker}Restart it.{marker}"])
            self.assertEqual(sp.fallback_text(headline), "*It is down.*")
        self.assertEqual(sp.split_answer("Use *one* of them. Next *two*."), ("Use one of them.", ["Next *two*."]))

    def test_an_in_word_double_marker_is_not_rebalanced_as_bold(self):
        headline, body = sp.split_answer("The env var DB__HOST is unset. Pods crashloop.")
        self.assertEqual(headline, "The env var DB__HOST is unset.")
        self.assertEqual(body, ["Pods crashloop."])
        headline, body = sp.split_answer("Memory is 2**20 bytes. Raise it.")
        self.assertEqual(headline, "Memory is 2**20 bytes.")
        self.assertEqual(body, ["Raise it."])

    def test_plain_keeps_paired_in_word_double_markers(self):
        self.assertEqual(sp.split_answer("DB__HOST or DB__PORT is unset.")[0], "DB__HOST or DB__PORT is unset.")
        self.assertEqual(sp._plain("Set DB__HOST and DB__PORT"), "Set DB__HOST and DB__PORT")
        self.assertEqual(sp._plain("2**20 and 2**30 bytes"), "2**20 and 2**30 bytes")
        self.assertEqual(sp._plain("**Checkout** and __payments__ are down"), "Checkout and payments are down")

    def test_plain_strips_in_word_star_bold_but_not_underscore_bold(self):
        self.assertEqual(sp._plain("**Pod**s are down"), "Pods are down")
        self.assertEqual(sp._plain("It re**start**ed twice"), "It restarted twice")
        self.assertEqual(sp._plain("**3**x faster"), "3x faster")
        self.assertEqual(sp._plain("snake__case__name"), "snake__case__name")
        self.assertEqual(sp._plain("x**2 and y**2"), "x**2 and y**2")
        headline, body = sp.split_answer("**Pod**s are down. Restart them.")
        self.assertEqual(headline, "Pods are down.")
        self.assertEqual(body, ["Restart them."])

    def test_more_abbreviations_do_not_end_the_headline(self):
        for line in ("Node pool np-1 at rev. 7 is cordoned.", "Certs expired Sept. 30 on seeded-a."):
            self.assertEqual(sp.split_answer(line), (line, []))

    def test_a_bold_lead_ends_at_its_closer_whatever_word_it_ends_on(self):
        for line, headline, body in (
            ("**Replicas are at max.** 3 pods run.", "Replicas are at max.", ["3 pods run."]),
            ("__Etc.__ Done.", "Etc.", ["Done."]),
            (
                "Yes — **replicas are at max.** 3 of 3 are ready. Nothing to do.",
                "Yes — replicas are at max.",
                ["3 of 3 are ready. Nothing to do."],
            ),
            ("Replicas are **at max.** 3 of 4 are ready.", "Replicas are at max.", ["3 of 4 are ready."]),
            ("*Replicas are at max.* 3 pods run.", "Replicas are at max.", ["3 pods run."]),
            ("The answer is _no._ 3 pods are down.", "The answer is no.", ["3 pods are down."]),
        ):
            self.assertEqual(sp.split_answer(line), (headline, body))
        self.assertEqual(sp.split_answer("Scale to max. 3 pods run."), ("Scale to max. 3 pods run.", []))

    def test_an_abbreviation_in_parentheses_does_not_end_the_headline(self):
        for line, headline in (
            ("Several pods fail (e.g. Checkout) in prod. Raise it.", "Several pods fail (e.g. Checkout) in prod."),
            ("Pods fail (i.e. Checkout). Raise it.", "Pods fail (i.e. Checkout)."),
        ):
            self.assertEqual(sp.split_answer(line)[0], headline)

    def test_no_before_a_number_and_ex_do_not_end_the_headline(self):
        # The incident card's headline is split_answer's first sentence of "What's wrong".
        for line, headline in (
            ("Ticket No. 3 is open. Second sentence.", "Ticket No. 3 is open."),
            ("payments-api fails readiness since deploy No. 42. Restore the secret.", "payments-api fails readiness since deploy No. 42."),
            ("See No. 3. Second sentence.", "See No. 3."),
            ("Filed under #No. 7 on seeded-a. Then wait.", "Filed under #No. 7 on seeded-a."),
            ("Use the default, ex. Checkout. More.", "Use the default, ex. Checkout."),
            ("Disk is at 90% (approx. Two hours left). Then it fills.", "Disk is at 90% (approx. Two hours left)."),
            ("Latency is high vs. Yesterday. Scale up.", "Latency is high vs. Yesterday."),
        ):
            with self.subTest(line=line):
                self.assertEqual(sp.split_answer(line)[0], headline)

    def test_max_min_and_no_end_a_sentence(self):
        for line, headline in (
            ("Replicas are at max. Raise the HPA ceiling.", "Replicas are at max."),
            ("Restarted after 5 min. Raise it.", "Restarted after 5 min."),
            ("The answer is no. Checkout is down.", "The answer is no."),
        ):
            self.assertEqual(sp.split_answer(line)[0], headline)

    def test_max_and_min_before_a_number_do_not_end_the_headline(self):
        for line, headline in (
            ("The pool is at its max. 4 pods are pending. Raise it.", "The pool is at its max. 4 pods are pending."),
            ("Keep min. 2 replicas on seeded-a. Then drain.", "Keep min. 2 replicas on seeded-a."),
        ):
            with self.subTest(line=line):
                self.assertEqual(sp.split_answer(line)[0], headline)

    def test_a_nul_in_the_answer_is_dropped(self):
        self.assertEqual(sp.split_answer("Hello \x005\x00 world. More."), ("Hello 5 world.", ["More."]))

    def test_a_nul_in_a_posture_is_dropped_before_its_parentheticals_are_held(self):
        # A literal NUL-number-NUL read as a held parenthetical: past the last it raised IndexError,
        # below it the parenthetical was swapped in.
        for clause, parts in (
            ("I scanned 3 clusters and seeded-a (x) was \x005\x00 unreachable.", ["seeded-a (x) was 5 unreachable."]),
            (
                "I scanned 3 clusters; seeded-c \x000\x00 could not be scanned (permission denied).",
                ["seeded-c 0 could not be scanned (permission denied)."],
            ),
        ):
            with self.subTest(clause=clause):
                self.assertEqual(sp.gap_parts(clause), parts)
    def test_an_answer_no_before_a_number_runs_into_the_next_sentence(self):
        # Known limit. Ending at "no." after "is" or ":" also cuts "Deploy is no. 1 priority." and
        # "We are no. 2 in the queue.", so "No." before a number stays a number, as "max." does.
        self.assertEqual(sp.split_answer("The answer is no. 3 pods are down.")[0], "The answer is no. 3 pods are down.")

    def test_many_abbreviations_stay_linear(self):
        line = "See e.g. A " * 4000 + "end."
        started = time.monotonic()
        sp.split_answer(line)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_long_backtick_run_stays_linear(self):
        for line in (
            "x " + "`" * BACKTICK_REPEATS,
            "x " + " ".join("`" * n for n in range(1, BACKTICK_REPEATS // 100)),
        ):
            for lay_out in (sp.split_answer, sp._plain):
                started = time.monotonic()
                lay_out(line)
                self.assertLess(time.monotonic() - started, 0.5, lay_out.__name__)

    def test_unclosed_bold_and_link_openers_stay_linear(self):
        for line in ("**a " * MARKUP_REPEATS, "__a " * MARKUP_REPEATS, "[a" * (2 * MARKUP_REPEATS)):
            for lay_out in (sp.split_answer, sp._plain):
                started = time.monotonic()
                lay_out(line)
                self.assertLess(time.monotonic() - started, 0.5, lay_out.__name__)

    def test_bold_markers_that_never_close_stay_linear(self):
        # 40 KB of "a **" or "a __" took 3.2 seconds while each bold ran to the line's end from every opener.
        for unit in ("a **", "a __"):
            line = unit * 10_000
            for render in (sp._plain, sp.to_mrkdwn):
                with self.subTest(unit=unit, render=render.__name__):
                    started = time.monotonic()
                    render(line)
                    self.assertLess(time.monotonic() - started, 2)

    def test_a_nul_in_the_answer_cannot_name_a_code_span(self):
        self.assertEqual(sp.split_answer("a \x005\x00 b. c"), ("a 5 b. c", []))
        self.assertEqual(sp._plain("`x` \x000\x00 and \x009\x00"), "x 0 and 9")

    def test_an_unmatched_backtick_run_is_literal(self):
        self.assertEqual(sp.split_answer("Run ```a` now. Next."), ("Run ```a` now.", ["Next."]))

    def test_known_wrong_sentence_ends(self):
        # Pinned as they are, not as they should be: "Mr." is not an abbreviation here, and a
        # closing quote or parenthesis does not end a sentence.
        for line in ('Done.) Next.', 'Done." Next.'):
            self.assertEqual(sp.split_answer(line), (line, []))
        self.assertEqual(sp.split_answer("Mr. Smith says hi. Next."), ("Mr.", ["Smith says hi. Next."]))

    def test_a_bold_ends_at_the_nearest_marker(self):
        # A run of three or more markers, or a "__" beside a letter that cannot close, leaves the text as written.
        for text in ("___x___", "******a.", "__foo__bar__"):
            with self.subTest(text=text):
                self.assertEqual(sp._plain(text), text)
                self.assertEqual(sp.to_mrkdwn(text), text)
        self.assertEqual(sp._plain("***bold*** and __init__"), "bold and init")

    def test_plain_leaves_code_spans_and_globs_alone(self):
        self.assertEqual(sp._plain("`__init__.py` is missing"), "__init__.py is missing")
        self.assertEqual(sp._plain("Delete `__pycache__` and **this**"), "Delete __pycache__ and this")
        self.assertEqual(sp._plain("Remove *.log,*.tmp"), "Remove *.log,*.tmp")

    def test_clip_keeps_the_start_of_a_long_word(self):
        clipped = sp._clip("apply Option B: " + "x" * 120, sp.BUTTON_TEXT_MAX)
        self.assertTrue(clipped.startswith("apply Option B: xxx"))
        self.assertLessEqual(len(clipped), sp.BUTTON_TEXT_MAX)
        self.assertEqual(sp._clip("word " * 30, 20), "word word word…")

    def test_plain_keeps_a_literal_star_and_drops_markup(self):
        self.assertEqual(sp._plain("Scale replicas 2*3 → 6"), "Scale replicas 2*3 → 6")
        self.assertEqual(sp._plain("Delete *.tmp under /var/cache"), "Delete *.tmp under /var/cache")
        self.assertEqual(sp._plain("**Bold**, *italic*, _also_ and `code`"), "Bold, italic, also and code")
        self.assertEqual(
            sp._plain("see [logs](https://x/query=(k8s_container)) now"), "see logs now"
        )

    def test_a_numbered_line_is_not_cut_at_its_number(self):
        headline, body = sp.split_answer("1. Checkout is crashlooping on OOM. It hit 256Mi.")
        self.assertEqual(headline, "Checkout is crashlooping on OOM.")
        self.assertEqual(body, ["It hit 256Mi."])

    def test_leading_tilde_block_has_no_headline(self):
        headline, body = sp.split_answer("~~~\nkubectl get pods\n~~~")
        self.assertEqual(headline, "")
        self.assertEqual(body, ["~~~\nkubectl get pods\n~~~"])

    def test_long_headline_clipped(self):
        headline, _ = sp.split_answer("word " * 100)
        self.assertLessEqual(len(headline), sp.HEADLINE_MAX)
        self.assertTrue(headline.endswith("…"))


class ButtonsTest(unittest.TestCase):
    def test_link_button(self):
        self.assertEqual(
            sp._button("Open PR ↗", "kage.link.0", url="https://github.com/o/r/pull/1"),
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open PR ↗", "emoji": True},
                "action_id": "kage.link.0",
                "url": "https://github.com/o/r/pull/1",
            },
        )
        self.assertRegex("kage.link.0", sp.LINK_ACTION_ID_PATTERN)

    def test_choice_button_value_is_label(self):
        button = sp._button("Raise to 512Mi", "triage.choice.0", value="Raise to 512Mi")
        self.assertEqual(button["value"], "Raise to 512Mi")
        self.assertNotIn("url", button)
        self.assertRegex(button["action_id"], sp.CHOICE_ACTION_ID_PATTERN)
        self.assertIsNone(sp.LINK_ACTION_ID_PATTERN.search(button["action_id"]))

    def test_a_long_choice_posts_only_what_its_button_shows(self):
        # A value longer than the label would carry words the user never saw
        # to any handler that reads it.
        choice = "look at: " + "word " * 40 + "HIDDEN-TAIL"
        actions = [b for b in sp.blocks_report("h", choices=[choice], action_id_prefix="kage") if b["type"] == "actions"]
        (button,) = actions[0]["elements"]
        self.assertEqual(button["value"], button["text"]["text"])
        self.assertLessEqual(len(button["value"]), sp.BUTTON_TEXT_MAX)
        self.assertNotIn("HIDDEN-TAIL", button["value"])

    def test_the_first_choice_is_primary_and_every_value_is_its_label(self):
        blocks = sp.blocks_report("h", choices=["Fix it", "", "See all 3"], action_id_prefix="kage")
        buttons = next(b for b in blocks if b["type"] == "actions")["elements"]
        self.assertEqual([(b["text"]["text"], b["value"]) for b in buttons], [("Fix it", "Fix it"), ("See all 3", "See all 3")])
        self.assertEqual(buttons[0]["style"], "primary")

    def test_buttons_wrap_at_five(self):
        rows = sp._actions([sp._button(str(i), f"kage.choice.{i}", value=str(i)) for i in range(7)])
        self.assertEqual([len(b["elements"]) for b in rows], [5, 2])
        self.assertEqual({b["type"] for b in rows}, {"actions"})

    def test_long_label_clipped(self):
        button = sp._button("word " * 40, "kage.choice.0", value="word " * 40)
        self.assertLessEqual(len(button["text"]["text"]), sp.BUTTON_TEXT_MAX)
        self.assertEqual(button["value"], "word " * 40)

    def test_a_url_past_slacks_limit_is_dropped(self):
        url = "https://x/" + "a" * (sp.BUTTON_URL_MAX - len("https://x/"))
        self.assertEqual(sp._button("Logs", "kage.link.0", url=url)["url"], url)
        self.assertIsNone(sp._button("Logs", "kage.link.0", url=url + "a"))

    def test_only_an_http_url_without_userinfo_reaches_a_button(self):
        # The host of https://console.cloud.google.com@evil.example/ is evil.example.
        for url in ("https://user:pass@host.example/x", "https://console.cloud.google.com@evil.example/logs",
                    "javascript:alert(1)", "data:text/html,<b>x</b>", "slack://open", "mailto:a@b.example",
                    "ftp://p/x", "https://github.com\\@evil.example/", "https://p\\evil.example/",
                    "https://p/x\n", "https://p /x", "https://", "https://[::1", "https://p/\x00x",
                    "https://p/\x7fx", "https://p/\u200bx", "https://p\u200b.example/"):
            self.assertIsNone(sp._button("Logs", "kage.link.0", url=url), url)
        # A scheme is case-insensitive, so an upper-case one is kept as written.
        for url in ("https://console.cloud.google.com/logs?q=a", "HTTPS://P", "http://p/a@b", "https://p?by=@me",
                    "https://p/#@x"):
            self.assertEqual(sp._button("Logs", "kage.link.0", url=url)["url"], url)

    def test_a_refused_url_gives_no_link_button_and_no_link(self):
        # A link button with no url still reads as a .link.<n> button: a click on it is acked and opens nothing.
        refused, kept = "https://github.com@evil.example/x", "https://p/a"
        self.assertIsNone(sp._button("Logs", "kage.link.0", url=refused))
        rows = sp._actions([sp._button("Logs", "kage.link.0", url=refused), sp._button("Docs", "kage.link.1", url=kept)])
        self.assertEqual([[(e["action_id"], e.get("url")) for e in r["elements"]] for r in rows], [[("kage.link.1", kept)]])
        self.assertEqual(sp._actions([sp._button("Logs", "kage.link.0", url=refused)]), [])
        self.assertEqual(sp.fallback_text("h", links=[("Logs", refused)]), "*h*")

    def test_a_link_with_an_unsafe_url_is_dropped_from_the_fallback(self):
        links = [("a", "javascript:alert(1)"), ("b", "https://github.com@evil.example/x"), ("c", "https://p/a@b")]
        self.assertEqual(sp.fallback_text("h", links=links), "*h*\n<https://p/a@b|c>")

class BlocksAnswerTest(unittest.TestCase):
    def test_headline_only(self):
        self.assertEqual(
            sp.blocks_answer("All three clusters are healthy."),
            [{"type": "section", "text": {"type": "mrkdwn", "text": "*All three clusters are healthy.*"}}],
        )

    def test_headline_escaped(self):
        blocks = sp.blocks_answer("a < b & c")
        self.assertEqual(blocks[0]["text"]["text"], "*a &lt; b &amp; c*")

    def test_link_buttons(self):
        blocks = sp.blocks_answer(
            "h", links=[("Open PR ↗", "https://github.com/o/r/pull/1"), {"text": "Files", "url": "https://f"}]
        )
        self.assertEqual(
            blocks[1],
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open PR ↗", "emoji": True},
                        "action_id": "kage.link.0",
                        "url": "https://github.com/o/r/pull/1",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Files", "emoji": True},
                        "action_id": "kage.link.1",
                        "url": "https://f",
                    },
                ],
            },
        )
        for button in blocks[1]["elements"]:
            self.assertRegex(button["action_id"], sp.LINK_ACTION_ID_PATTERN)

    def test_choice_buttons_value_is_label(self):
        blocks = sp.blocks_answer("h", choices=["Raise to 512Mi", "Leave it"], action_id_prefix="triage")
        elements = blocks[1]["elements"]
        self.assertEqual([e["value"] for e in elements], ["Raise to 512Mi", "Leave it"])
        self.assertEqual([e["action_id"] for e in elements], ["triage.choice.0", "triage.choice.1"])
        self.assertNotIn("url", elements[0])
        for button in elements:
            self.assertIsNone(sp.LINK_ACTION_ID_PATTERN.search(button["action_id"]))

    def test_buttons_wrap_at_five(self):
        blocks = sp.blocks_answer("h", choices=[str(i) for i in range(7)])
        self.assertEqual([len(b["elements"]) for b in blocks[1:]], [5, 2])
        ids = [e["action_id"] for b in blocks[1:] for e in b["elements"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_long_label_clipped(self):
        button = sp.blocks_answer("h", choices=["word " * 40])[1]["elements"][0]
        self.assertLessEqual(len(button["text"]["text"]), sp.BUTTON_TEXT_MAX)
        self.assertEqual(button["value"], "word " * 40)

    def test_order(self):
        blocks = sp.blocks_answer("h", links=[("l", "https://l")], choices=["c"])
        self.assertEqual([b["type"] for b in blocks], ["section", "actions", "actions"])
        self.assertIn("url", blocks[1]["elements"][0])
        self.assertIn("value", blocks[2]["elements"][0])


class PrimaryLinkTest(unittest.TestCase):
    def test_only_the_first_link_is_primary_and_only_when_asked(self):
        links = [("Open PR ↗", "https://github.com/a/b/pull/1"), ("Files changed ↗", "https://github.com/a/b/pull/1/files")]
        styled = [e.get("style") for e in sp.blocks_answer("h", links=links, primary_link=True)[1]["elements"]]
        self.assertEqual(styled, ["primary", None])
        plain = sp.blocks_answer("h", links=links, choices=["yes"])
        self.assertNotIn("style", str(plain))


BAR_HEAD = {"type": "section", "text": {"type": "mrkdwn", "text": "*h*"}}
BAR_REST = [{"type": "context", "elements": []}, {"type": "actions", "elements": []}]


class SideBarTest(unittest.TestCase):
    def test_all_but_the_headline_go_beside_the_bar(self):
        self.assertEqual(
            sp.with_side_bar([BAR_HEAD, *BAR_REST], sp.SIDE_BAR_GREEN, "h"),
            {"blocks": [BAR_HEAD], "attachments": [{"color": "#2EB67D", "fallback": "h", "blocks": BAR_REST}]},
        )

    def test_nothing_to_bar_sends_an_empty_attachment_list(self):
        # chat.update keeps a message's attachments unless it is sent some.
        self.assertEqual(sp.with_side_bar([BAR_HEAD], sp.SIDE_BAR_YELLOW, "h"), {"blocks": [BAR_HEAD], "attachments": []})
        self.assertEqual(sp.with_side_bar([], sp.SIDE_BAR_YELLOW, ""), {"blocks": [], "attachments": []})

    def test_message_blocks_reads_its_own_then_its_side_bars(self):
        unfurl = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "preview"}}]}
        message = {"blocks": [BAR_HEAD], "attachments": ["junk", unfurl, {"color": "ECB22E", "blocks": BAR_REST}]}
        self.assertEqual(sp.message_blocks(message), [BAR_HEAD, *BAR_REST])
        self.assertEqual(sp.message_blocks({"blocks": [BAR_HEAD]}), [BAR_HEAD])
        self.assertEqual(sp.message_blocks(None), [])

    def test_side_bar_color_restores_the_hash_slack_drops(self):
        self.assertEqual(sp.side_bar_color({"attachments": [{"color": "ECB22E", "blocks": BAR_REST}]}), "#ECB22E")
        self.assertEqual(sp.side_bar_color({"attachments": [{"color": "#2EB67D", "blocks": BAR_REST}]}), "#2EB67D")

    def test_no_side_bar_without_an_attachment_holding_blocks(self):
        # A link unfurl is an attachment with no blocks; it is not a bar.
        for message in ({}, {"attachments": [{"color": "ECB22E"}]}, {"attachments": [{"blocks": BAR_REST}]}, None):
            self.assertIsNone(sp.side_bar_color(message), message)


class FallbackTextTest(unittest.TestCase):
    def test_same_layout_as_mrkdwn(self):
        text = sp.fallback_text(
            "Two findings.",
            rows=[{"text": "seeded-a: OOM", "severity": "warning"}],
            links=[("Open PR", "https://p")],
            choices=["Yes", "No"],
        )
        self.assertEqual(text, "*Two findings.*\n`warning` seeded-a: OOM\n<https://p|Open PR>\nReply with one of: Yes · No")

    def test_rows_are_mrkdwn(self):
        text = sp.fallback_text("h", rows=[{"text": "**seeded-a**: see [PR](https://p)"}, "plain `x`"])
        self.assertEqual(text, "*h*\n• *seeded-a*: see <https://p|PR>\n• plain `x`")

    def test_rows_cannot_mention_anyone(self):
        text = sp.fallback_text("h", rows=[{"text": "<!channel> & <@U1>", "severity": "<!here>"}])
        self.assertNotIn("<!", text)
        self.assertNotIn("<@", text)
        self.assertIn("&lt;!channel&gt; &amp; &lt;@U1&gt;", text)

    def test_a_row_link_cannot_become_a_mention(self):
        rows = [{"text": "ask [bob](@U024BE7LH) or [team](!channel) in [ops](#C1)"}, "[PR](https://p)"]
        text = sp.fallback_text("h", rows=rows)
        self.assertNotIn("<@", text)
        self.assertNotIn("<!", text)
        self.assertNotIn("<#", text)
        self.assertIn("• ask bob or team in ops", text)
        self.assertIn("<https://p|PR>", text)
        nested = [
            "[[x](@U1)](!channel)",
            "**[minor] c [[z](#a)](@U024BE7LH)**",
            "[[[y](!here)](#C1)](@U2)",
            "[[w](https://p)](!here)",
        ]
        for row in nested:
            with self.subTest(row=row):
                line = sp.fallback_text("h", rows=[row]).splitlines()[1]
                self.assertNotIn("<@", line)
                self.assertNotIn("<!", line)
                self.assertNotIn("<#", line)

    def test_a_rich_text_link_that_is_not_a_web_url_is_text(self):
        elements = sp._rich_elements("see [bob](@U1) and [PR](https://p)")
        self.assertNotIn("@U1", [e.get("url") for e in elements])
        self.assertIn({"type": "text", "text": "bob"}, elements)
        self.assertIn({"type": "link", "url": "https://p", "text": "PR"}, elements)

    def test_a_nul_in_a_row_is_dropped(self):
        self.assertEqual(sp.fallback_text("h", rows=["a \x000\x00 b"]), "*h*\n• a 0 b")

    def test_urls_keep_their_underscores_and_asterisks(self):
        cases = {
            "[docs](https://x.io/__init__/y)": "<https://x.io/__init__/y|docs>",
            "see https://x.io/a/**b**/c now": "see https://x.io/a/**b**/c now",
            "**https://x.io/a_b_**": "*https://x.io/a_b_*",
            "[**docs**](https://x.io/__a__)": "<https://x.io/__a__|*docs*>",
        }
        for row, line in cases.items():
            with self.subTest(row=row):
                self.assertEqual(sp.fallback_text("h", rows=[row]).splitlines()[1], sp.BULLET + line)

    def test_rich_text_bolds_what_the_text_fallback_bolds(self):
        cases = {
            "2**20 and 2**30 pods pending": [{"type": "text", "text": "2**20 and 2**30 pods pending"}],
            "** not bold **": [{"type": "text", "text": "** not bold **"}],
            "**Pod**s down": [{"type": "text", "text": "Pod", "style": {"bold": True}}, {"type": "text", "text": "s down"}],
            "re**start**ed": [
                {"type": "text", "text": "re"},
                {"type": "text", "text": "start", "style": {"bold": True}},
                {"type": "text", "text": "ed"},
            ],
            "__enforce__ PSA": [{"type": "text", "text": "enforce", "style": {"bold": True}}, {"type": "text", "text": " PSA"}],
            "snake__case__name": [{"type": "text", "text": "snake__case__name"}],
        }
        for row, elements in cases.items():
            with self.subTest(row=row):
                self.assertEqual(sp._rich_elements(row), elements)
                bold = [e["text"] for e in elements if e.get("style")]
                self.assertEqual(bold, [text for groups in sp.MD_BOLD.findall(row) for text in groups if text])

    def test_a_rich_text_link_keeps_parentheses_in_its_url(self):
        elements = sp._rich_elements("[w](https://en.wikipedia.org/wiki/Foo_(bar)) after")
        self.assertEqual(elements[0], {"type": "link", "url": "https://en.wikipedia.org/wiki/Foo_(bar)", "text": "w"})
        self.assertEqual(elements[1], {"type": "text", "text": " after"})

    def test_a_rich_text_link_that_is_not_a_safe_url_is_text(self):
        elements = sp._rich_elements("[a](https://x.io|y) [b](javascript:alert)")
        self.assertNotIn("link", [e["type"] for e in elements])
        self.assertIn({"type": "text", "text": "a"}, elements)
        self.assertIn({"type": "text", "text": "b"}, elements)

    def test_empty_rows_are_skipped(self):
        self.assertEqual(sp.fallback_text("h", rows=["", "  ", {"text": ""}, "x"]), "*h*\n• x")

    def test_labels_cannot_mention_anyone(self):
        text = sp.fallback_text("h", links=[("<!here>", "https://p")], choices=["<@U1> & <!channel>", "No"])
        self.assertNotIn("<!", text)
        self.assertNotIn("<@", text)
        self.assertIn("&lt;@U1&gt; &amp; &lt;!channel&gt;", text)


    def test_urls_cannot_mention_anyone_or_break_the_link(self):
        links = [("x", "!channel"), ("y", "@U123"), ("z", "https://a?b=>c|d"), ("ok", "https://p")]
        self.assertEqual(sp.fallback_text("h", links=links), "*h*\n<https://a?b=%3Ec%7Cd|z> · <https://p|ok>")

    def test_a_url_slack_would_refuse_is_dropped(self):
        too_long = "https://x/" + "a" * sp.BUTTON_URL_MAX
        links = [("a", too_long), ("b", "https://p\n"), ("c", "https://"), ("d", "HTTPS://P")]
        self.assertEqual(sp.fallback_text("h", links=links), "*h*\n<HTTPS://P|d>")

    def test_a_url_with_userinfo_is_not_a_link(self):
        # The host of https://console.cloud.google.com@evil.example/ is evil.example.
        for url in ("https://console.cloud.google.com@evil.example/", "https://user:pw@evil.example/x",
                    "HTTPS://u@evil.example"):
            self.assertIsNone(sp.SAFE_URL.fullmatch(url), url)
            self.assertEqual(sp.fallback_text("h", links=[("Logs", url)]), "*h*", url)
            self.assertNotEqual(sp._rich_elements(f"[d]({url})")[0]["type"], "link", url)
            self.assertNotIn(f"<{url}|", sp.to_mrkdwn(f"[d]({url})"), url)
        # An @ after the host is path, query or fragment, and stays a link.
        for url in ("https://p/a@b", "https://p?by=@me", "https://p/#@x"):
            self.assertIsNotNone(sp.SAFE_URL.fullmatch(url), url)
    def test_a_link_url_cannot_end_the_link_early(self):
        text = sp.fallback_text("h", links=[("Logs", "https://p/?q=a|b>c<d&e")])
        self.assertIn("<https://p/?q=a%7Cb%3Ec%3Cd&amp;e|Logs>", text)

    def test_an_entity_in_a_link_url_is_kept_as_written(self):
        text = sp.fallback_text("h", links=[("Logs", "https://p/?q=a&lt;b")])
        self.assertIn("<https://p/?q=a&amp;lt;b|Logs>", text)

    def test_a_headline_with_markup_keeps_every_character_and_loses_the_bold(self):
        self.assertEqual(sp.fallback_text("It is *down* now"), "It is *down* now")
        self.assertEqual(sp.fallback_text("~gone~ and _soft_ stuff"), "~gone~ and _soft_ stuff")
        self.assertEqual(sp.fallback_text("Remove *.log,*.tmp"), "Remove *.log,*.tmp")
        self.assertEqual(sp.fallback_text("Scale to ~3 pods."), "Scale to ~3 pods.")
        for headline in ("__init__.py is missing.", "Scale replicas 2*3 → 6", "DB_HOST is unset"):
            self.assertEqual(sp.fallback_text(headline), f"*{headline}*")

    def test_a_command_a_code_span_kept_reaches_the_fallback_whole(self):
        for answer, kept in (("Run `rm -rf ~/x*` here.", "Run rm -rf ~/x* here."),
                             ("Delete `*.tmp` now.", "Delete *.tmp now.")):
            headline, _body = sp.split_answer(answer)
            self.assertEqual(headline, kept)
            self.assertEqual(sp.fallback_text(headline), kept)


class LinkAckTest(unittest.TestCase):
    def test_ack_does_nothing_else(self):
        ack = mock.AsyncMock()
        body = mock.Mock()
        asyncio.run(sp.ack_link_click(ack, body, {"action_id": "kage.link.0"}))
        ack.assert_awaited_once_with()
        self.assertEqual(body.mock_calls, [])


class BlocksReportTest(unittest.TestCase):
    ROWS = [
        {"severity": "critical", "text": "seeded-b and -c admit privileged pods"},
        {"severity": "critical", "text": "default SA is `cluster-admin` on seeded-c"},
    ]

    def _report(self, **kwargs):
        args = dict(
            headline="Security & RBAC audit: 7 findings, 2 critical.",
            note="2 are new since the last run.",
            rows=self.ROWS,
            choices=["look at: seeded-b and -c admit privileged pods"],
            links=[("Ledger issue #231 ↗", "https://l/231")],
            action_id_prefix="kage_audit",
        )
        args.update(kwargs)
        return sp.blocks_report(**args)

    def test_headline_group_then_the_buttons(self):
        blocks = self._report()
        self.assertEqual([b["type"] for b in blocks], ["rich_text", "divider", "rich_text", "divider", "actions"])
        self.assertEqual(
            blocks[0]["elements"][0]["elements"],
            [
                {"type": "text", "text": "Security & RBAC audit: 7 findings, 2 critical.", "style": {"bold": True}},
                {"type": "text", "text": " 2 are new since the last run."},
            ],
        )

    def test_the_rows_have_no_header_and_lead_with_a_code_tag(self):
        first, second = self._report()[2]["elements"]
        self.assertEqual(
            first["elements"],
            [
                {"type": "text", "text": "critical", "style": {"code": True}},
                {"type": "text", "text": " "},
                {"type": "text", "text": "seeded-b and -c admit privileged pods"},
            ],
        )
        self.assertIn({"type": "text", "text": "cluster-admin", "style": {"code": True}}, second["elements"])
        self.assertNotIn("•", str(first) + str(second))

    def test_primary_choice_first_then_the_link(self):
        buttons = self._report()[4]["elements"]
        self.assertEqual([b["action_id"] for b in buttons], ["kage_audit.choice.0", "kage_audit.link.0"])
        self.assertEqual(buttons[0]["style"], "primary")
        self.assertEqual(buttons[0]["value"], "look at: seeded-b and -c admit privileged pods")
        self.assertEqual(buttons[1]["url"], "https://l/231")
        self.assertNotIn("style", buttons[1])
        self.assertTrue(sp.CHOICE_ACTION_ID_PATTERN.search(buttons[0]["action_id"]))
        self.assertTrue(sp.LINK_ACTION_ID_PATTERN.search(buttons[1]["action_id"]))

    def test_detail_is_a_plain_line_under_the_headline(self):
        head = self._report(detail="Security audit: 2 new across 3 clusters")[0]["elements"]
        self.assertEqual(len(head), 2)
        self.assertEqual(head[1]["elements"], [{"type": "text", "text": "Security audit: 2 new across 3 clusters"}])

    def test_empty_parts_are_omitted(self):
        self.assertEqual(
            [b["type"] for b in sp.blocks_report("h")], ["rich_text"]
        )

    def test_rows_with_no_text_and_no_severity_are_skipped(self):
        self.assertEqual(sp.blocks_report("h", rows=["", "  "]), sp.blocks_report("h"))
        group = sp.blocks_report("h", rows=["", {"text": "", "severity": "major"}, "x"])[2]["elements"]
        self.assertEqual(group[0]["elements"][0]["text"], "major")
        self.assertEqual(len(group), 2)
        for section in group:
            self.assertTrue(section["elements"])

    def test_after_rows_is_a_plain_line_below_the_rows(self):
        blocks = self._report(after_rows="Also found: 18 more.")
        self.assertEqual([b["type"] for b in blocks], ["rich_text", "divider", "rich_text", "divider", "rich_text", "actions"])
        self.assertEqual(len(blocks[0]["elements"]), 1)
        self.assertEqual(blocks[4]["elements"][0]["elements"], [{"type": "text", "text": "Also found: 18 more."}])
        self.assertEqual(self._report(after_rows="  "), self._report())
        long_line = self._report(after_rows="**x** " * 200)[4]["elements"][0]["elements"][0]["text"]
        self.assertLessEqual(len(long_line), sp.ROW_TEXT_MAX)
        self.assertNotIn("**", long_line)

    def test_a_row_is_clipped_to_row_text_max(self):
        text = self._report(rows=["x" * (sp.ROW_TEXT_MAX + 50)])[2]["elements"][0]["elements"][0]["text"]
        self.assertLessEqual(len(text), sp.ROW_TEXT_MAX)

    def test_detail_is_plained_and_clipped(self):
        detail = self._report(detail="**" + "y" * (sp.ROW_TEXT_MAX + 50) + "**")[0]["elements"][1]["elements"][0]["text"]
        self.assertLessEqual(len(detail), sp.ROW_TEXT_MAX)
        self.assertNotIn("*", detail)

    def test_names_gap_reads_a_negation_as_no_gap(self):
        self.assertTrue(sp.names_gap("seeded-c could not be scanned"))
        self.assertTrue(sp.names_gap("No drift, but 2 clusters were unreachable"))
        self.assertFalse(sp.names_gap("No clusters were unreachable"))
        self.assertFalse(sp.names_gap("with no unreachable nodes"))
        self.assertFalse(sp.names_gap("Clusters skipped: none"))
        self.assertFalse(sp.names_gap("unreachable", ": 0."))
        self.assertFalse(sp.names_gap("Skipped", ": 0 clusters"))
        self.assertFalse(sp.names_gap("unreachable", ": 0/3."))
        self.assertFalse(sp.names_gap("Clusters skipped", ": 0 of 3 clusters."))
        self.assertEqual(sp.gap_parts("Skipped: 0 clusters"), [])
        self.assertTrue(sp.names_gap("seeded-b unreachable", ": 2 nodes."))
        self.assertTrue(sp.names_gap("seeded-b skipped: no credentials"))
        self.assertTrue(sp.names_gap("unreachable", ": 0 of 3 control planes answered."))

    def test_names_gap_reads_a_long_clause_in_linear_time(self):
        started = time.monotonic()
        self.assertFalse(sp.names_gap("I scanned 3 clusters" + " no unreachable" * 3000))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_names_gap_reads_a_far_negation_as_a_gap(self):
        # The negation is read only within sp.NEGATION_REACH of the gap word; past it the
        # clause errs toward showing a gap rather than hiding one.
        self.assertFalse(sp.names_gap("seeded-c no" + " " * 90 + "unreachable"))
        self.assertTrue(sp.names_gap("seeded-c no" + " " * 300 + "unreachable"))

    def test_names_gap_does_not_read_a_url(self):
        self.assertFalse(sp.names_gap("see https://x.example/unreachable for details"))
        self.assertTrue(sp.names_gap("seeded-b unreachable, see https://x.example/runbook"))

    def test_gap_parts_keeps_every_gap_and_leaves_the_counts(self):
        for text, parts in (
            ("1 skipped, 2 unreachable", ["1 skipped", "2 unreachable"]),
            ("3 new, 1 resolved, 1 skipped", ["1 skipped"]),
            ("2 new and 1 skipped", ["1 skipped"]),
            ("2 new but 1 unreachable", ["1 unreachable"]),
            ("I scanned 3 clusters, but seeded-d was unreachable", ["seeded-d was unreachable"]),
            ("No drift (seeded-d unreachable, 5 namespaces clean)", ["seeded-d unreachable"]),
            ("seeded-c could not be scanned (permission denied)", ["seeded-c could not be scanned (permission denied)"]),
            ("unreachable: none", []),
            ("I scanned 3 clusters and 41 workloads", []),
        ):
            with self.subTest(text=text):
                self.assertEqual(sp.gap_parts(text), parts)

    def test_gap_parts_leaves_the_counts_before_a_gap_that_opens_on_a_name(self):
        for text, parts in (
            ("7 findings across 3 clusters but seeded-c was not reached", ["seeded-c was not reached"]),
            ("2 new and seeded-c skipped", ["seeded-c skipped"]),
            ("2 new (a and b) and seeded-c skipped", ["seeded-c skipped"]),
            ("I scanned 3 clusters and 41 workloads but seeded-c was not reached", ["seeded-c was not reached"]),
            ("seeded-d and seeded-e were skipped", ["seeded-d and seeded-e were skipped"]),
            ("seeded-3 and seeded-4 were skipped", ["seeded-3 and seeded-4 were skipped"]),
            ("2 clusters and seeded-c were unreachable", ["2 clusters and seeded-c were unreachable"]),
            ("3 namespaces and seeded-c were not scanned", ["3 namespaces and seeded-c were not scanned"]),
            ("1 node pool and seeded-c were skipped", ["1 node pool and seeded-c were skipped"]),
            ("1 of 3 clusters and seeded-c weren't reached", ["1 of 3 clusters and seeded-c weren't reached"]),
            ("2 namespaces were denied and seeded-c was skipped", ["2 namespaces were denied and seeded-c was skipped"]),
            ("1 skipped and seeded-e unreachable", ["1 skipped and seeded-e unreachable"]),
            ("7 findings across 3 clusters but seeded-c were not reached", ["seeded-c were not reached"]),
            ("I scanned 3 clusters and they were unreachable", ["they were unreachable"]),
        ):
            with self.subTest(text=text):
                self.assertEqual(sp.gap_parts(text), parts)

    def test_gap_parts_reads_denied_only_for_a_scan_subject_or_access(self):
        for text, parts in (
            ("5 namespaces denied", ["5 namespaces denied"]),
            ("2 namespaces were denied and seeded-c was skipped", ["2 namespaces were denied and seeded-c was skipped"]),
            ("access denied on seeded-c", ["access denied on seeded-c"]),
            ("12 requests were denied by the admission policy", []),
            ("2 images are forbidden", []),
            ("5 denied requests in the audit log", []),
            ("2 forbidden images", []),
            ("4 access denied events", []),
            ("5 pods forbidden from privileged mode", []),
            ("3 service accounts denied by policy", []),
        ):
            with self.subTest(text=text):
                self.assertEqual(sp.gap_parts(text), parts)

    def test_gap_parts_keeps_the_names_a_colon_lists_but_not_a_reason(self):
        for text, parts in (
            ("skipped: seeded-d (no credentials).", ["skipped: seeded-d (no credentials)."]),
            ("2 clusters could not be scanned: seeded-d, seeded-e.", ["2 clusters could not be scanned: seeded-d, seeded-e."]),
            ("seeded-b unreachable: no response from the control plane.", ["seeded-b unreachable"]),
            ("skipped: seeded-d, 2 new", ["skipped: seeded-d"]),
        ):
            with self.subTest(text=text):
                self.assertEqual(sp.gap_parts(text), parts)

    def test_gap_parts_stays_linear_on_long_runs(self):
        # A 96 KB run of spaces took 84 seconds while every pattern starting on whitespace retried mid-run.
        run = 96_000
        for text in (
            " " * run,
            "a" + " " * run + "x",
            "a" + " " * run + "(b",
            "could not be scanned" + " " * run + "x",
            "could not be scanned" + " and" * (run // 4),
            "1 x could not be scanned" + " and y" * (run // 6),
            "2 new" + " and 1 skipped" * (run // 14),
        ):
            with self.subTest(text=text[:24]):
                started = time.monotonic()
                sp.gap_parts(text)
                self.assertLess(time.monotonic() - started, 2)

    def test_as_line_keeps_a_name_lowercase(self):
        self.assertEqual(sp.as_line("2 clusters were unreachable"), "2 clusters were unreachable.")
        self.assertEqual(sp.as_line("but some were skipped"), "But some were skipped.")
        self.assertEqual(sp.as_line("seeded-c could not be scanned"), "seeded-c could not be scanned.")
        self.assertEqual(sp.as_line("Done!"), "Done!")

    def test_row_links_keep_only_urls_slack_accepts(self):
        too_long = "https://x/" + "a" * sp.BUTTON_URL_MAX
        for url, linked in ((too_long, False), ("HTTPS://P/q", True), ("https://", False)):
            elements = sp._rich_elements(f"[d]({url})")
            self.assertEqual(elements[0]["type"] == "link", linked, url)
            self.assertEqual(f"<{url}|d>" in sp.to_mrkdwn(f"[d]({url})"), linked, url)

    def test_a_row_link_and_a_button_take_the_same_longest_url(self):
        for scheme in ("https://", "http://"):
            longest = scheme + "x/" + "a" * (sp.BUTTON_URL_MAX - len(scheme) - len("x/"))
            with self.subTest(scheme=scheme):
                self.assertTrue(sp.SAFE_URL.fullmatch(longest))
                self.assertFalse(sp.SAFE_URL.fullmatch(longest + "a"))
                self.assertTrue(sp._safe_link_url(longest))
                self.assertFalse(sp._safe_link_url(longest + "a"))

    def test_mrkdwn_reads_fences_as_the_paragraph_splitter_does(self):
        # A tilde fence's lines pass through; a leading triple-backtick span is a code span.
        self.assertEqual(sp.to_mrkdwn("~~~\n**raw**\n~~~\n**bold**"), "~~~\n**raw**\n~~~\n*bold*")
        self.assertEqual(sp.to_mrkdwn("```kubectl``` output **exposed**"), "```kubectl``` output *exposed*")
        self.assertEqual(sp.to_mrkdwn("```\n**raw**\n```\n**bold**"), "```\n**raw**\n```\n*bold*")

    def test_a_row_link_with_parentheses_in_its_url_is_whole_in_both_views(self):
        url = "https://console.cloud.google.com/logs/query;query=resource.type%3D(k8s_container)"
        self.assertEqual(sp._rich_elements(f"[logs]({url})"), [{"type": "link", "url": url, "text": "logs"}])
        self.assertEqual(sp.to_mrkdwn(f"[logs]({url})"), f"<{url}|logs>")

    def test_a_row_detail_is_a_second_line_in_both_views(self):
        row = {"severity": "critical", "text": "privileged pods", "detail": "Any pod can reach the node; **enforce** PSA."}
        section = self._report(rows=[row])[2]["elements"][0]["elements"]
        self.assertEqual(section[3], {"type": "text", "text": "\n"})
        self.assertIn({"type": "text", "text": "enforce", "style": {"bold": True}}, section)
        self.assertEqual(
            sp.fallback_text("h", rows=[row]), "*h*\n`critical` privileged pods\nAny pod can reach the node; *enforce* PSA."
        )

    def test_a_long_row_and_detail_are_clipped_alike_in_both_views(self):
        long = "x" * (sp.ROW_TEXT_MAX + 50)
        _, row_line, detail_line = sp.fallback_text("h", rows=[{"text": long, "detail": long}]).split("\n")
        self.assertEqual(row_line, sp.BULLET + sp._clip(long, sp.ROW_TEXT_MAX))
        self.assertEqual(detail_line, sp._clip(long, sp.ROW_TEXT_MAX))


class SeverityRowTest(unittest.TestCase):
    def test_tag_then_text(self):
        self.assertEqual(sp.severity_row("critical", "x"), "`critical` x")
        self.assertFalse(hasattr(sp, "SEVERITY_MARKERS"))

    def test_any_severity_is_tagged_and_cannot_break_the_code_span(self):
        self.assertEqual(sp.fallback_text("h", rows=[{"text": "t", "severity": "cri`tical"}]), "*h*\n`critical` t")


if __name__ == "__main__":
    unittest.main()
