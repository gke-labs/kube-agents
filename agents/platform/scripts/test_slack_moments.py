"""Tests for slack_moments, the opened-PR and needs-you layout."""

import ast
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_moments as m
import slack_presenter as p

PR = "https://github.com/acme/fleet-config/pull/412"
#: A reason holding a run of this many backticks took 7 seconds to present.
BACKTICK_REPEATS = 20_000


def _buttons(blocks):
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _contexts(blocks):
    return [b["elements"][0]["text"] for b in blocks if b["type"] == "context"]


class StandaloneTest(unittest.TestCase):
    def test_imports_nothing_from_the_gateway_or_slack(self):
        tree = ast.parse(Path(m.__file__).read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots - {"__future__", "re", "collections", "slack_presenter"}, set())


class OpenedPrTest(unittest.TestCase):
    def test_finds_a_pr_on_a_line_that_says_it_was_opened(self):
        text = f"Checked the limits.\nOpened {PR}/files for review."
        self.assertEqual(m.opened_pr(text), (PR, "fleet-config", "412", f"Opened {PR}/files for review."))

    def test_each_verb_counts_and_case_does_not(self):
        for verb in ("opened", "Created", "RAISED", "filed", "submitted"):
            self.assertIsNotNone(m.opened_pr(f"{verb} {PR}"), verb)

    def test_a_cited_pr_is_not_announced(self):
        self.assertIsNone(m.opened_pr(f"{PR} already covers this"))
        self.assertIsNone(m.opened_pr(f"See {PR}; reopening is not needed"))

    def test_the_verb_must_be_on_the_line_with_the_url(self):
        self.assertIsNone(m.opened_pr(f"Opened the file.\n{PR}"))

    def test_the_verb_must_come_before_the_url(self):
        self.assertIsNone(m.opened_pr(f"regression came from {PR}, opened by bob last week"))
        self.assertIsNone(m.opened_pr(f"Opened the runbook; {PR} explains it"))

    def test_a_pr_or_pull_request_between_the_verb_and_the_url(self):
        for lead in ("Opened PR", "created a pull request:", "Raised a new PR", "filed the PR"):
            self.assertEqual(m.opened_pr(f"{lead} {PR}")[0], PR, lead)
        self.assertEqual(m.opened_pr(f"Opened <{PR}|PR #412>")[0], PR)
        self.assertEqual(m.opened_pr(f"Opened [PR #412]({PR})")[0], PR)
        self.assertEqual(m.opened_pr(f"Opened PR #412: {PR}")[0], PR)
        for lead in ("I opened PR #412 (", "Opened PR #412 — ", "Opened PR #412 - ", "Opened pull request #412 ("):
            self.assertEqual(m.opened_pr(f"{lead}{PR}) to raise the limit")[0], PR, lead)

    def test_a_negated_verb_is_not_an_opened_pr(self):
        for lead in ("I have not opened", "Never opened", "I haven't yet opened a PR:", "didn't create"):
            self.assertIsNone(m.opened_pr(f"{lead} {PR}"), lead)

    def test_someone_else_as_the_subject_is_not_ours(self):
        for lead in (
            "Dependabot opened", "alice created", "bob has opened", "Renovate just opened",
            "Dependabot then opened", "Alice reviewed and opened", "@bob and opened", "bob successfully opened",
            "bob reviewed and opened", "Kube Agents Robot then opened", "Fred reviewed and opened",
            "Tests passed. Renovate rebased and opened", "the bot then opened",
            "dependabot[bot] opened", "renovate[bot] just opened", "Kelly opened", "Emily created",
            "Kelly reviewed and opened", "`dependabot[bot]` opened", "**Dependabot** opened",
            "Renovate (bot) opened", "<@U123> opened", "_renovate_ opened", "Ahmed then opened",
            "Fred then opened", "Mohammed and opened", "Ted and then opened", "Done. Dependabot opened",
            "Fred reviewed it, and opened",
        ):
            self.assertIsNone(m.opened_pr(f"{lead} {PR} to bump the base image"), lead)

    def test_our_own_subject_or_steps_still_count(self):
        for lead in (
            "I opened", "We've opened", "I have just opened", "Re-ran the suite and opened PR", "- Opened",
            "✅ Opened", "Done: opened", "Successfully created PR #412:", "Successfully opened",
            "I have successfully opened", "Finally opened", "Done! I’ve opened", "I have now also opened",
            "Fixed and opened", "Checked the limits, then opened", "I reviewed and opened",
            "Just fixed it and then opened", "Tests passed. Rebuilt the image and opened", "We've tested and opened",
            "Bumped the image tag in `values.yaml` and opened", "Superseded https://github.com/acme/x/pull/300 and opened",
            "Bumped the chart to 1.4.2 and opened", "Fixed main.py:42 and then opened", "I then opened",
            "Then I opened", "**Done:** opened", "Checked it: opened",
        ):
            self.assertEqual(m.opened_pr(f"{lead} {PR}")[0], PR, lead)

    def test_a_long_line_is_clipped_under_the_headline(self):
        line = f"Opened {PR} " + "because " * 1000
        blocks, _ = m.pr_opened(*m.opened_pr(line))
        self.assertLessEqual(len(_contexts(blocks)[0]), m.EVIDENCE_MAX)
        self.assertTrue(_contexts(blocks)[0].endswith(p.ELLIPSIS))

    def test_the_opened_pr_is_found_after_a_cited_one(self):
        other = "https://github.com/acme/x/pull/300"
        self.assertEqual(m.opened_pr(f"Following up {other}, opened {PR}")[0], PR)

    def test_an_issue_url_is_not_a_pr(self):
        self.assertIsNone(m.opened_pr("Opened https://github.com/acme/fleet-config/issues/9"))

    def test_empty_and_none(self):
        self.assertIsNone(m.opened_pr(""))
        self.assertIsNone(m.opened_pr(None))


class PrOpenedTest(unittest.TestCase):
    def test_headline_evidence_and_two_url_buttons(self):
        blocks, text = m.pr_opened(PR, "fleet-config", "412", f"Opened PR {PR} raising the limit")
        self.assertEqual(blocks[0]["text"]["text"], "*I opened PR #412 in fleet-config. It's yours to review.*")
        self.assertEqual(_contexts(blocks), ["Opened PR #412 raising the limit"])
        buttons = _buttons(blocks)
        self.assertEqual([b["text"]["text"] for b in buttons], ["Open PR ↗", "Files changed ↗"])
        self.assertEqual([b["url"] for b in buttons], [PR, PR + "/files"])
        self.assertTrue(all(p.LINK_ACTION_ID_PATTERN.search(b["action_id"]) for b in buttons))
        self.assertTrue(all(b["action_id"].startswith("kage_pr.") for b in buttons))
        self.assertEqual(text.split("\n")[1], "Opened PR #412 raising the limit")
        self.assertIn(PR + "/files", text)

    def test_a_link_around_the_url_is_shortened_whole(self):
        for line in (f"Opened <{PR}|PR #412> raising it", f"Opened [PR #412]({PR}) raising it"):
            blocks, _ = m.pr_opened(PR, "fleet-config", "412", line)
            self.assertEqual(_contexts(blocks), ["Opened PR #412 raising it"], line)

    def test_a_tail_after_the_number_goes_with_the_url(self):
        for tail in ("/files", "?diff=split", "#discussion_r1", "/files#diff-1"):
            blocks, _ = m.pr_opened(PR, "fleet-config", "412", f"Opened {PR}{tail} for review.")
            self.assertEqual(_contexts(blocks), ["Opened PR #412 for review."], tail)
        blocks, _ = m.pr_opened(PR, "fleet-config", "412", f"Opened [PR #412]({PR}/files) for review.")
        self.assertEqual(_contexts(blocks), ["Opened PR #412 for review."])

    def test_a_number_before_the_url_is_not_repeated(self):
        for line in (f"I opened PR #412 ({PR}) to raise it", f"I opened PR #412 — {PR} to raise it",
                     f"I opened PR #412: {PR} to raise it"):
            blocks, _ = m.pr_opened(PR, "fleet-config", "412", line)
            self.assertEqual(_contexts(blocks), ["I opened PR #412 to raise it"], line)

    def test_the_workers_line_cannot_mention_anyone(self):
        blocks, text = m.pr_opened(PR, "fleet-config", "412", f"Opened {PR} <!channel>")
        self.assertEqual(_contexts(blocks), ["Opened PR #412 &lt;!channel&gt;"])
        self.assertNotIn("<!channel>", text)


class NeedsYouTest(unittest.TestCase):
    def test_a_long_backtick_run_stays_fast(self):
        started = time.monotonic()
        m.needs_you("Which one? " + "`" * BACKTICK_REPEATS)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_listed_options_become_choice_buttons(self):
        reason = "Which checkout-gateway did you mean?\nTwo clusters run one. Which?\n- seeded-reliability\n2) seeded-debug"
        blocks, text = m.needs_you(reason)
        self.assertEqual(blocks[0]["text"]["text"], "*Which checkout-gateway did you mean?*")
        self.assertEqual(_contexts(blocks), ["Two clusters run one. Which?", m.WAITING])
        buttons = _buttons(blocks)
        self.assertEqual([b["text"]["text"] for b in buttons], ["seeded-reliability", "seeded-debug"])
        self.assertTrue(all(p.CHOICE_ACTION_ID_PATTERN.search(b["action_id"]) for b in buttons))
        self.assertEqual(blocks[-1]["block_id"], p.WAITING_BLOCK_ID)
        self.assertEqual(text.split("\n")[:2], ["*Which checkout-gateway did you mean?*", "Two clusters run one. Which?"])
        self.assertIn("seeded-debug", text)

    def test_option_markup_is_not_on_the_button(self):
        blocks, _ = m.needs_you("Which cluster?\n- `seeded-reliability`\n- **seeded-debug**")
        self.assertEqual([b["text"]["text"] for b in _buttons(blocks)], ["seeded-reliability", "seeded-debug"])

    def test_a_glob_or_dunder_option_keeps_its_characters(self):
        blocks, _ = m.needs_you("Which pods?\n- Delete app=web-*\n- Keep `__pycache__`\n- Scale to 2*3")
        self.assertEqual([b["text"]["text"] for b in _buttons(blocks)], ["Delete app=web-*", "Keep __pycache__", "Scale to 2*3"])

    def test_a_glob_or_dunder_headline_keeps_its_characters(self):
        for reason, title in (
            ("Scale app=web-* to 0?\n- Yes\n- No", "Scale app=web-* to 0?"),
            ("Delete __pycache__ from the image?\n- Yes\n- No", "Delete __pycache__ from the image?"),
            ("Delete `app=web-*` & **all** its pods?", "Delete app=web-* & all its pods?"),
        ):
            blocks, text = m.needs_you(reason)
            self.assertEqual(blocks[0]["text"], {"type": "plain_text", "text": title, "emoji": True}, reason)
            self.assertEqual(text.split("\n")[0], title.replace("&", "&amp;"), reason)
            self.assertEqual(m.needs_you_settled(blocks)[0], blocks[0])

    def test_a_headline_the_presenter_keeps_whole_stays_bold(self):
        blocks, text = m.needs_you("Restart the **prod** pods in `web`?\n- Yes\n- No")
        self.assertEqual(blocks[0]["text"], {"type": "mrkdwn", "text": "*Restart the prod pods in web?*"})
        self.assertEqual(text.split("\n")[0], "*Restart the prod pods in web?*")

    def test_a_list_after_the_question_is_not_choices(self):
        reason = "Should I restart it?\nI found:\n- pod a is OOMKilled\n- pod b is Pending"
        blocks, _ = m.needs_you(reason)
        self.assertEqual(_buttons(blocks), [])
        self.assertEqual(_contexts(blocks)[0], "I found:\n- pod a is OOMKilled\n- pod b is Pending")

    def test_a_list_that_does_not_end_the_reason_is_not_choices(self):
        blocks, _ = m.needs_you("Which cluster?\n- seeded-a\n- seeded-b\nThe preflight failed on both.")
        self.assertEqual(_buttons(blocks), [])

    def test_the_question_can_follow_the_headline(self):
        blocks, _ = m.needs_you("Preflight failed.\nDetails here.\nWhich should I use?\n\n- seeded-a\n- seeded-b")
        self.assertEqual([b["text"]["text"] for b in _buttons(blocks)], ["seeded-a", "seeded-b"])
        self.assertEqual(_contexts(blocks)[0], "Details here.\nWhich should I use?")

    def test_no_list_is_text_only(self):
        blocks, text = m.needs_you("Which namespace should I scale?")
        self.assertEqual(_buttons(blocks), [])
        self.assertEqual(_contexts(blocks), [m.WAITING])
        self.assertEqual(text, "*Which namespace should I scale?*")

    def test_a_first_line_of_markup_alone_does_not_head_the_question(self):
        blocks, text = m.needs_you("```\nkubectl says 3 clusters\nWhich cluster?\n- alpha\n- beta")
        self.assertEqual(blocks[0]["text"]["text"], "*kubectl says 3 clusters*")
        self.assertEqual([b["value"] for b in _buttons(blocks)], ["alpha", "beta"])
        self.assertEqual(_contexts(blocks), ["Which cluster?", m.WAITING])
        self.assertIn("alpha", text)
        blocks, _ = m.needs_you("**\nWhich cluster?")
        self.assertEqual(blocks[0]["text"]["text"], "*Which cluster?*")
        self.assertEqual(_contexts(blocks), [m.WAITING])
        self.assertIsNone(m.needs_you("```\n**"))

    def test_a_first_line_of_tildes_or_underscores_does_not_head_the_question(self):
        for fence in ("~~~", "___"):
            with self.subTest(fence=fence):
                blocks, _ = m.needs_you(fence + "\nWhich cluster?")
                self.assertEqual(blocks[0]["text"]["text"], "*Which cluster?*")
        self.assertIsNone(m.needs_you("~~~\n___"))

    def test_an_unpaired_backtick_stays_bold(self):
        blocks, text = m.needs_you("Should I run `kubectl rollout restart on web?")
        self.assertEqual(blocks[0]["text"], {"type": "mrkdwn", "text": "*Should I run `kubectl rollout restart on web?*"})
        self.assertEqual(text, "*Should I run `kubectl rollout restart on web?*")

    def test_no_buttons_keeps_the_options_in_the_text(self):
        reason = "Which checkout-gateway did you mean?\n- seeded-reliability\n- seeded-debug"
        blocks, text = m.needs_you(reason, buttons=False)
        self.assertEqual(_buttons(blocks), [])
        self.assertEqual(_contexts(blocks), ["- seeded-reliability\n- seeded-debug", m.WAITING])
        self.assertIn("seeded-debug", text)

    def test_one_option_or_too_many_stay_in_the_text(self):
        for count in (1, p.BUTTONS_PER_ROW + 1):
            options = [f"- option {n}" for n in range(count)]
            blocks, _ = m.needs_you("\n".join(["Pick one?", *options]))
            self.assertEqual(_buttons(blocks), [], count)
            self.assertIn("option 0", _contexts(blocks)[0])

    def test_an_option_too_long_for_a_button_keeps_all_of_them_in_the_text(self):
        long = "x" * (p.BUTTON_TEXT_MAX + 1)
        blocks, _ = m.needs_you(f"Pick one?\n- short\n- {long}")
        self.assertEqual(_buttons(blocks), [])
        self.assertIn(long, _contexts(blocks)[0])

    def test_a_long_first_line_is_repeated_whole_below_the_clipped_headline(self):
        first = "why " * 60
        blocks, _ = m.needs_you(first)
        self.assertLessEqual(len(blocks[0]["text"]["text"]), p.HEADLINE_MAX + 2)
        self.assertEqual(_contexts(blocks)[0], first.strip())

    def test_detail_is_clipped(self):
        blocks, _ = m.needs_you("Question\n" + "y" * (m.DETAIL_MAX * 2))
        detail = _contexts(blocks)[0]
        self.assertEqual(len(detail), m.DETAIL_MAX)
        self.assertTrue(detail.endswith(p.ELLIPSIS))

    def test_the_reason_cannot_mention_anyone(self):
        blocks, text = m.needs_you("Question\n<@U123> said so. Which?\n- <!here>\n- <!channel>")
        self.assertEqual(_contexts(blocks)[0], "&lt;@U123&gt; said so. Which?")
        for mention in ("<@U123>", "<!here>", "<!channel>"):
            self.assertNotIn(mention, text)

    def test_an_empty_reason_is_no_question(self):
        self.assertIsNone(m.needs_you(""))
        self.assertIsNone(m.needs_you("  \n "))

    def test_settled_drops_the_choices_and_the_waiting_line_only(self):
        blocks, _ = m.needs_you("Which cluster?\nTwo run it. Which?\n- seeded-a\n- seeded-b")
        settled = m.needs_you_settled(blocks)
        self.assertEqual(_buttons(settled), [])
        self.assertEqual(settled, [b for b in blocks if b["type"] != "actions"][:-1])
        self.assertEqual(_contexts(settled), ["Two run it. Which?"])

    def test_settled_keeps_a_link_beside_the_choices(self):
        link = {"type": "button", "action_id": "kage.link.0", "url": "https://example.com"}
        choice = {"type": "button", "action_id": "kage_needs.choice.0"}
        settled = m.needs_you_settled([{"type": "actions", "elements": [choice, link]}])
        self.assertEqual(settled, [{"type": "actions", "elements": [link]}])


if __name__ == "__main__":
    unittest.main()
