"""Tests for slack_status, the plan and session renderer."""

import ast
import sys
import time
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_status as s

#: Slack's cap on a message's text, and how long a title of one that size may take.
SLACK_MESSAGE_MAX = 40_000
LINEAR_BUDGET_SECONDS = 1.0
#: Characters agents.sessions.rename answered invalid_name for, one per call, on a live
#: workspace (2026-10-02). Each one alone refuses the whole title.
RENAME_REFUSED = (
    "/:·…∕—–‒―−•→×<>#@\\*~`%;[]{}+$^‘’²½Ⅳ\u00a0"
    "ªºʰ𝐀©®™⌘←∑°§¶€£¥ﬁǅ〜、。！？¿¡«»⬆〒🀄⌚⏰⭐〽ㅋ\u0e33\u200d\u200b\U0001f1ef"
)
#: Words and symbols it accepted on the same workspace, each of which a title keeps as is.
RENAME_ACCEPTED = (
    "नमस्ते",
    "ไม\u0e48",
    "q\u0303",
    "क\u093c",
    "ب\u064b",
    "ラーメン",
    "々",
    "ʻ",
    "ꙮ",
    "ᄀ",
    "٣",
    "𠀀",
    "✅",
    "🚨",
    "⚠\ufe0f",
    "★",
    "✓",
    "👍\U0001f3fd",
    "1\ufe0f\u20e3",
)
#: Code points the every-character check walks: the Basic Multilingual Plane and the two
#: supplementary planes that hold letters and emoji, less the surrogate block.
CHECKED_END = 0x2FFFF
SURROGATES = range(0xD800, 0xE000)


def _row(task_id="t_a", title="check payments", lines=(), status=s.TASK_RUNNING):
    return SimpleNamespace(task_id=task_id, title=title, lines=list(lines), status=status)


class StandaloneTest(unittest.TestCase):
    def test_imports_nothing_from_the_gateway_or_slack(self):
        tree = ast.parse(Path(s.__file__).read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots - {"__future__", "re", "unicodedata", "collections", "typing"}, set())


class TaskStatusTest(unittest.TestCase):
    def test_every_mapped_kind_is_a_block_kit_status(self):
        for kind in s.TASK_STATUS_BY_KIND:
            self.assertIn(s.task_status(kind), s.TASK_STATUSES, kind)

    def test_mapping(self):
        self.assertEqual(s.task_status("heartbeat"), "in_progress")
        self.assertEqual(s.task_status("completed"), "complete")
        self.assertEqual(s.task_status("blocked"), "pending")
        self.assertEqual(s.task_status("unblocked"), "in_progress")
        self.assertEqual(s.task_status("gave_up"), "error")
        self.assertIsNone(s.task_status("archived"))
        self.assertIsNone(s.task_status("status"))

    def test_retried_kinds_leave_the_row_and_a_block_loop_waits(self):
        # As slack_presenter reads them: the dispatcher retries a crash or a timeout.
        self.assertIsNone(s.task_status("crashed"))
        self.assertIsNone(s.task_status("timed_out"))
        self.assertEqual(s.task_status("block_loop_detected"), "pending")


class TaskCardTest(unittest.TestCase):
    def test_shape(self):
        card = s.task_card("t_a", "check payments", ["reading logs", "reading metrics"], s.TASK_RUNNING)
        self.assertEqual(
            {k: card[k] for k in ("type", "task_id", "title", "status")},
            {"type": "task_card", "task_id": "t_a", "title": "check payments", "status": "in_progress"},
        )
        self.assertEqual(card["details"]["type"], "rich_text")
        items = card["details"]["elements"][0]["elements"]
        self.assertEqual([i["elements"][0]["text"] for i in items], ["✓ reading logs", "◌ reading metrics"])

    def test_a_settled_card_has_no_open_step(self):
        card = s.task_card("t_a", "x", ["a", "b"], s.TASK_COMPLETE)
        texts = [i["elements"][0]["text"] for i in card["details"]["elements"][0]["elements"]]
        self.assertTrue(all(t.startswith(s.STEP_DONE) for t in texts))

    def test_no_notes_no_details(self):
        self.assertNotIn("details", s.task_card("t_a", "x", ["", "  "], s.TASK_RUNNING))

    def test_blank_title_falls_back_to_the_id(self):
        self.assertEqual(s.task_card("t_a", " ", [], s.TASK_RUNNING)["title"], "t_a")

    def test_unknown_status_reads_as_running(self):
        self.assertEqual(s.task_card("t_a", "x", [], "weird")["status"], s.TASK_RUNNING)

    def test_only_the_last_steps_are_kept(self):
        card = s.task_card("t_a", "x", [f"n{i}" for i in range(10)], s.TASK_RUNNING)
        items = card["details"]["elements"][0]["elements"]
        self.assertEqual(len(items), s.STEPS_MAX)
        self.assertEqual(items[-1]["elements"][0]["text"], "◌ n9")

    def test_long_text_is_clipped(self):
        card = s.task_card("t_a", "word " * 100, ["word " * 100], s.TASK_RUNNING)
        self.assertLessEqual(len(card["title"]), s.ROW_TITLE_MAX)
        step = card["details"]["elements"][0]["elements"][0]["elements"][0]["text"]
        self.assertLessEqual(len(step), s.STEP_TEXT_MAX + 2)


class PlanTest(unittest.TestCase):
    def test_the_plan_is_one_block_with_no_stop(self):
        # Stop is deferred: /stop would end the turn and leave the cards running.
        for status in (s.TASK_RUNNING, s.TASK_PENDING):
            blocks = s.plan_blocks(None, [_row(status=status)])
            self.assertEqual([b["type"] for b in blocks], ["plan"])

    def test_title(self):
        self.assertEqual(s.plan_title("is it up?", [_row(), _row("t_b")]), "is it up?")
        self.assertEqual(s.plan_title(None, [_row()]), "check payments")
        self.assertEqual(s.plan_title("", [_row(), _row("t_b")]), "2 cards")

    def test_rows_are_capped_oldest_first(self):
        rows = [_row(f"t_{i}") for i in range(s.ROWS_MAX + 5)]
        tasks = s.plan_blocks(None, rows)[0]["tasks"]
        self.assertEqual(len(tasks), s.ROWS_MAX)
        self.assertEqual(tasks[-1]["task_id"], rows[-1].task_id)

    def test_text(self):
        rows = [_row(lines=["a", "b"]), _row("t_b", "check seeded-b", status=s.TASK_ERROR)]
        self.assertEqual(s.plan_text("is it up?", rows), "is it up?\n◌ check payments · b\n✗ check seeded-b")

    def test_text_escapes_slack_markup(self):
        # The text field is parsed: an unescaped "<!here>" broadcasts and a "<url|label>" relabels a link.
        rows = [_row(title="R&D: roll back", lines=["see <!here> <https://evil.example|docs>"])]
        self.assertEqual(
            s.plan_text(None, rows),
            "R&amp;D: roll back\n◌ R&amp;D: roll back · see &lt;!here&gt; &lt;https://evil.example|docs&gt;",
        )
        blocks = s.plan_blocks(None, rows)
        self.assertEqual(blocks[0]["title"], "R&D: roll back", "blocks carry rich text, which Slack does not parse")


class SessionTest(unittest.TestCase):
    def test_status(self):
        self.assertEqual(s.session_status("is thinking..."), s.SESSION_PROCESSING)
        self.assertEqual(s.session_status(""), s.SESSION_CLOSED)
        self.assertEqual(s.session_status("closed"), s.SESSION_CLOSED)

    def test_title(self):
        self.assertEqual(s.session_title("<@U1> is <#C1|prod> ok: <https://a.b/c>"), "is prod ok")
        self.assertLessEqual(len(s.session_title("x" * 200)), s.TITLE_MAX)

    def test_a_long_word_after_a_short_one_is_cut_not_dropped(self):
        self.assertEqual(s.session_title("Restart " + "n" * 90), "Restart " + "n" * 69 + s.TITLE_ELLIPSIS)
        self.assertEqual(s.session_title("word " * 40), ("word " * 15).rstrip() + s.TITLE_ELLIPSIS)

    def test_title_keeps_what_real_markup_gives(self):
        cases = {
            "is <@U123> ok with <#C1|ops> and <https://x.example/a|the doc> or <https://y.example>?": (
                "is ok with ops and the doc or ?"
            ),
            "check <!here> <!subteam^S1|@oncall> kube-system/coredns: now": "check kube-system-coredns, now",
        }
        for ask, title in cases.items():
            with self.subTest(ask=ask):
                self.assertEqual(s.session_title(ask), title)

    def test_a_slash_between_words_joins_them_whatever_their_spelling(self):
        cases = {
            "cafe\u0301/bar restarts": "caf\u00e9-bar restarts",
            "kube-system\uff0fcoredns restarts": "kube-system-coredns restarts",
        }
        for ask, title in cases.items():
            with self.subTest(ask=ask):
                self.assertEqual(s.session_title(ask), title)

    def test_a_typographic_hyphen_stays_a_hyphen(self):
        for hyphen in ("\u2010", "\u2011"):
            with self.subTest(char=f"U+{ord(hyphen):04X}"):
                self.assertEqual(s.session_title(f"kube{hyphen}system restarts"), "kube-system restarts")

    def test_a_slash_after_a_word_ending_in_a_mark_joins_it(self):
        for left in ("\u0939\u0948", "\u0e43\u0e0a\u0e48", "\u0915\u093f", "q\u0303"):
            with self.subTest(word=left):
                self.assertEqual(s.session_title(f"{left}/coredns ok"), f"{left}-coredns ok")

    def test_a_slash_beside_a_dropped_character_does_not_join(self):
        self.assertEqual(s.session_title("pods \u2182/x ok"), "pods x ok")

    def test_marks_on_a_refused_character_go_with_it(self):
        cases = {
            "\u2b50\ufe0f deploy": "deploy",
            "#\ufe0f\u20e3 rollout": "rollout",
            "\u203c\ufe0f\u20e3/x now": "!! x now",
            "\u26a0\ufe0f deploy": "\u26a0\ufe0f deploy",
        }
        for ask, title in cases.items():
            with self.subTest(ask=ask):
                self.assertEqual(s.session_title(ask), title)

    def test_no_character_slack_refused_survives(self):
        for refused in RENAME_REFUSED:
            with self.subTest(char=f"U+{ord(refused):04X}"):
                self.assertNotIn(refused, s.session_title(f"pods {refused} restarts"))

    def test_what_slack_accepted_is_kept(self):
        for accepted in RENAME_ACCEPTED:
            with self.subTest(word=accepted):
                self.assertEqual(s.session_title(f"pods {accepted} ok"), f"pods {accepted} ok")

    def test_no_title_character_has_a_compatibility_form(self):
        # Every letter Slack refused has one (ª, ﬁ, 𝐀, ำ). This checks the output against
        # that pattern, not against the rule that built it.
        survivors = set()
        for code in range(CHECKED_END + 1):
            if code not in SURROGATES:
                survivors.update(s.session_title(f"a{chr(code)}b"))
        compatibility = sorted(c for c in survivors if unicodedata.normalize("NFKC", c) != c)
        self.assertEqual(compatibility, [])

    def test_dashes_ellipses_and_quotes_become_ascii(self):
        cases = {
            "pods — restarts": "pods - restarts",
            "wait…": "wait...",
            "check /metrics on a/b": "check metrics on a-b",
            "what’s down?": "what's down?",
            "cpu at 90% × 3": "cpu at 90 percent x 3",
            "café": "café",
            "cafe\u0301": "café",
            "x² ﬁle": "x2 file",
            "are pods ok、yes。": "are pods ok, yes.",
            "👨\u200d👩 🇯🇵 team": "👨👩 team",
        }
        for ask, title in cases.items():
            with self.subTest(ask=ask):
                self.assertEqual(s.session_title(ask), title)

    def test_a_clipped_title_ends_in_ascii_within_the_limit(self):
        ask = (
            "Check the platform-agent-host cluster: are any pods outside kube-system not Running, "
            "and is any node under memory pressure?"
        )
        title = s.session_title(ask)
        self.assertEqual(title, "Check the platform-agent-host cluster, are any pods outside kube-system not...")
        self.assertLessEqual(len(title), s.TITLE_MAX)

    def test_title_stays_linear_on_unclosed_markup(self):
        # It runs on the gateway's event loop: a pattern that rescans to the end from every
        # "<" took 29 s on a 40 KB ask of bare "<", stalling every thread.
        for unit in ("<", "<!", "<@", "<#A|", "<a|"):
            with self.subTest(unit=unit):
                ask = unit * (SLACK_MESSAGE_MAX // len(unit))
                start = time.monotonic()
                s.session_title(ask)
                self.assertLess(time.monotonic() - start, LINEAR_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
