"""Tests for slack_moments, the opened-PR and needs-you layout."""

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_moments as m
import slack_presenter as p

PR = "https://github.com/acme/fleet-config/pull/412"


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

    def test_i_or_we_after_a_leading_clause_is_ours(self):
        for lead in (
            "Tests pass, so I opened", "Once CI was green I opened", "Done — I opened",
            "Dependabot's PR was stale and I opened", "Fix verified - I've opened", "It built, and we have opened",
        ):
            self.assertEqual(m.opened_pr(f"{lead} {PR}")[0], PR, lead)

    def test_a_label_before_the_url_is_an_opened_pr(self):
        for line in (
            f"Opened: {PR}", f"Opened: <{PR}>", f"PR opened: {PR}", f"**Opened PR:** <{PR}>",
            f"*Opened PR* {PR}", f"**Opened:** [PR #412]({PR})", f"`Created` {PR}",
        ):
            self.assertEqual(m.opened_pr(line)[0], PR, line)
        self.assertIsNone(m.opened_pr(f"Bob's PR opened: {PR}"))

    def test_a_labels_evidence_keeps_its_markup_and_drops_the_url(self):
        blocks, _ = m.pr_opened(*m.opened_pr(f"**Opened PR:** <{PR}>"))
        self.assertEqual(_contexts(blocks)[0], "*Opened PR #412*")

    def test_markup_wrapped_round_the_url_goes_with_it(self):
        for line in (f"Opened PR **{PR}**", f"Opened PR **<{PR}>**", f"Opened PR `{PR}`", f"Opened `{PR}`"):
            blocks, _ = m.pr_opened(*m.opened_pr(line))
            self.assertEqual(_contexts(blocks)[0], "Opened PR #412", line)
        blocks, _ = m.pr_opened(*m.opened_pr(f"**Done, opened PR {PR}**"))
        self.assertEqual(_contexts(blocks)[0], "*Done, opened PR #412*")

    def test_every_opened_pr_in_a_text_is_found(self):
        other = "https://github.com/acme/other/pull/7"
        found = list(m.opened_prs(f"- Opened {PR}\n- Opened {other}, and opened {PR}/files"))
        self.assertEqual([f[0] for f in found], [PR, other, PR])
        self.assertEqual(list(m.opened_prs(f"{PR} already covers it")), [])

    def test_another_link_on_the_pr_line_stays_text(self):
        blocks, _ = m.pr_opened(*m.opened_pr(f"Opened PR {PR}, see [docs](https://other.example/x)"))
        self.assertEqual(_contexts(blocks)[0], "Opened PR #412, see [docs](https://other.example/x)")

    def test_a_draft_pr_is_an_opened_pr(self):
        for line in (f"Opened a draft PR {PR}", f"Opened a new draft PR: {PR}"):
            self.assertIsNotNone(m.opened_pr(line), line)

    def test_words_between_pr_and_the_url_in_one_sentence_still_count(self):
        for lead in (
            "Opened PR #412 in acme/x:",
            "Opened PR #412 for the memory limit:",
            "Opened PR #412 against main:",
            "Opened a new draft PR for the limit:",
        ):
            self.assertEqual(m.opened_pr(f"{lead} {PR}")[0], PR, lead)
        self.assertIsNone(m.opened_pr(f"Bob opened PR #412 against main: {PR}"))
        self.assertIsNone(m.opened_pr(f"Opened PR #412 against main. The fix is {PR}"))

    def test_a_pr_the_sentence_only_cites_is_not_the_opened_one(self):
        for line in (
            f"Opened PR #500, which reverts {PR}",
            f"Opened a PR to supersede {PR}",
            f"Opened PR #500 as a follow-up to {PR}",
            f"Opened PR #500 in acme/x: {PR}",
        ):
            self.assertIsNone(m.opened_pr(line), line)

    def test_a_name_before_a_colon_or_comma_is_someone_else(self):
        self.assertIsNone(m.opened_pr(f"Dependabot: opened {PR}"))
        self.assertIsNone(m.opened_pr(f"Renovate, as usual, opened {PR}"))
        for lead in ("We, as usual, opened", "✅, opened", "✅: opened"):
            self.assertEqual(m.opened_pr(f"{lead} {PR}")[0], PR, lead)

    def test_another_subject_after_our_first_step_is_not_ours(self):
        for lead in (
            "Checked with Bob and he then opened", "Confirmed with Alice, who then opened",
            "Reviewed Alice's branch, which she then opened", "Asked the team and they opened",
            "Ran the bot, which then opened",
        ):
            self.assertIsNone(m.opened_pr(f"{lead} {PR}"), lead)

    def test_a_long_line_is_clipped_under_the_headline(self):
        line = f"Opened {PR} " + "because " * 1000
        blocks, _ = m.pr_opened(*m.opened_pr(line))
        self.assertLessEqual(len(_contexts(blocks)[0]), m.EVIDENCE_MAX)
        self.assertTrue(_contexts(blocks)[0].endswith(p.ELLIPSIS))

    def test_a_name_ending_in_ed_is_someone_else(self):
        for line in (
            f"Fred: opened {PR}", f"Mohammed, as usual, opened {PR}", f"Triggered by Renovate, opened {PR}",
            f"Triggered by Renovate and opened {PR}", f"Rotated by cert-manager: opened {PR}",
        ):
            self.assertIsNone(m.opened_pr(line), line)

    def test_our_own_outcome_and_step_labels_still_open_a_pr(self):
        for line in (
            f"Resolved: opened {PR}", f"Tests passed, opened {PR}", f"Working on it, opened {PR}",
            f"Addressed the review, opened {PR}", f"Drained node-a and opened {PR}", f"Rotated the cert, opened {PR}",
            f"Cordoned node-b: opened {PR}", f"Fixed by bumping the chart, opened {PR}",
            f"Fixed by hand, opened {PR}", f"Scaled by 2x and opened {PR}",
        ):
            self.assertEqual(m.opened_pr(line)[0], PR, line)

    def test_the_opened_pr_is_found_after_a_cited_one(self):
        other = "https://github.com/acme/x/pull/300"
        self.assertEqual(m.opened_pr(f"Following up {other}, opened {PR}")[0], PR)

    def test_an_issue_url_is_not_a_pr(self):
        self.assertIsNone(m.opened_pr("Opened https://github.com/acme/fleet-config/issues/9"))

    def test_a_pr_off_github_or_over_http_is_not_announced(self):
        for url in (
            "http://evil.example/acme/payments/pull/42", "https://evil.example/acme/payments/pull/42",
            "http://github.com/acme/payments/pull/42", "https://github.com.evil.example/acme/payments/pull/42",
            "https://evilgithub.com/acme/payments/pull/42",
        ):
            self.assertIsNone(m.opened_pr(f"Opened {url} to fix the probe."), url)

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

    def test_a_marker_inside_a_word_keeps_the_headline_bold(self):
        # Slack reads neither as markup, and the fallback keeps both bold too.
        for reason, title in (("Which node_pool should I drain?", "Which node_pool should I drain?"), ("Scale to 2*3 replicas?", "Scale to 2*3 replicas?")):
            blocks, text = m.needs_you(reason + "\n- Yes\n- No")
            self.assertEqual(blocks[0]["text"], {"type": "mrkdwn", "text": f"*{title}*"}, reason)
            self.assertEqual(text.split("\n")[0], f"*{title}*", reason)

    def test_a_list_after_the_question_is_not_choices(self):
        reason = "Should I restart it?\nI found:\n- pod a is OOMKilled\n- pod b is Pending"
        blocks, _ = m.needs_you(reason)
        self.assertEqual(_buttons(blocks), [])
        self.assertEqual(_contexts(blocks)[0], "I found:\n- pod a is OOMKilled\n- pod b is Pending")

    def test_a_plan_after_a_proceed_question_is_not_choices(self):
        for question in (
            "Shall I proceed?", "OK to go ahead?", "Should I continue?", "Do you approve?", "Sound good?",
            "Does this plan look right?", "Does the fix look good to you?", "Does the new plan look right?", "Do these look right?", "Looks OK?", "OK?", "Any objections?",
        ):
            reason = f"Here is the fix. {question}\n1. Drain node-pool-a\n2. Upgrade to 1.31\n3. Uncordon"
            blocks, text = m.needs_you(reason)
            self.assertEqual(_buttons(blocks), [], question)
            self.assertIn("1. Drain node-pool-a", text, question)
        for question in ("Looks good, what next?", "Looks OK, but where should I start?", "Thoughts?"):
            blocks, _ = m.needs_you(f"{question}\n- Drain\n- Upgrade")
            self.assertEqual(len(_buttons(blocks)), 2, question)
        blocks, _ = m.needs_you("Which step should I proceed with?\n- Drain\n- Upgrade")
        self.assertEqual([b["text"]["text"] for b in _buttons(blocks)], ["Drain", "Upgrade"])

    def test_a_question_asking_which_way_to_go_on_keeps_its_buttons(self):
        for question in (
            "How would you like to proceed?",
            "How should we continue?",
            "Can you approve one of these fixes?",
            "Should I proceed with a rollback or a scale-up?",
            "Which fix should I go ahead with?",
        ):
            blocks, _ = m.needs_you(f"The rollout stalled. {question}\n- Roll back\n- Scale up")
            self.assertEqual([b["text"]["text"] for b in _buttons(blocks)], ["Roll back", "Scale up"], question)

    def test_an_emphasised_question_or_a_lettered_list_keeps_its_buttons(self):
        for reason in (
            "**Which cluster should I drain?**\n- seeded-a\n- seeded-b",
            "*Which cluster should I drain?*\n- seeded-a\n- seeded-b",
            "Which cluster should I drain?\nA) seeded-a\nB) seeded-b",
            "Which cluster should I drain?\na. seeded-a\nb. seeded-b",
        ):
            blocks, _ = m.needs_you(reason)
            self.assertEqual([b["text"]["text"] for b in _buttons(blocks)], ["seeded-a", "seeded-b"], reason)

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

    def test_a_fence_opener_with_a_language_does_not_head_the_question(self):
        blocks, _ = m.needs_you("```bash\nWhich namespace?\n- default\n- prod")
        self.assertEqual(blocks[0]["text"]["text"], "*Which namespace?*")
        self.assertEqual([b["value"] for b in _buttons(blocks)], ["default", "prod"])

    def test_a_markdown_link_in_the_detail_is_a_slack_link(self):
        blocks, _ = m.needs_you("Which?\nSee [runbook](https://x.example/a?b=1&c=2) or `[no](https://y.example)`.")
        self.assertEqual(
            blocks[1]["elements"][0]["text"],
            "See <https://x.example/a?b=1&amp;c=2|runbook> or `[no](https://y.example)`.",
        )

    def test_bold_in_the_detail_is_slack_bold_outside_code(self):
        blocks, _ = m.needs_you("Which pod should I restart?\n**web-1** is OOMKilled, not `**x**`.\n- web-1\n- web-2")
        self.assertEqual(blocks[1]["elements"][0]["text"], "*web-1* is OOMKilled, not `**x**`.\n- web-1\n- web-2")

    def test_a_dunder_or_a_star_inside_bold_keeps_its_characters(self):
        for detail in (
            "See https://github.com/a/b/blob/main/pkg/__init__.py now",
            "__DB_HOST__ unset",
            "**/tmp/*.log** filled",
            "re**start**ed",
        ):
            blocks, _ = m.needs_you(f"Which?\n{detail}")
            self.assertEqual(blocks[1]["elements"][0]["text"], detail)

    def test_a_link_that_is_not_safe_stays_escaped_text(self):
        blocks, _ = m.needs_you("Which?\nSee [<@U1>](javascript:alert) now.")
        self.assertEqual(blocks[1]["elements"][0]["text"], "See [&lt;@U1&gt;](javascript:alert) now.")

    def test_a_link_label_cannot_mention_anyone(self):
        blocks, _ = m.needs_you("Which?\nSee [<!channel>](https://x.example).")
        self.assertEqual(blocks[1]["elements"][0]["text"], "See <https://x.example|&lt;!channel&gt;>.")

    def test_a_nul_in_the_detail_is_dropped_rather_than_read_as_a_code_span(self):
        blocks, _ = m.needs_you("Which?\nA \x000\x00 b")
        self.assertEqual(blocks[1]["elements"][0]["text"], "A 0 b")

    def test_a_question_opening_with_a_code_span_heads_itself(self):
        blocks, text = m.needs_you("```checkout-gateway``` is in two clusters. Which?\n- seeded-a\n- seeded-b")
        self.assertIn("is in two clusters. Which?", blocks[0]["text"]["text"])
        self.assertEqual([b["value"] for b in _buttons(blocks)], ["seeded-a", "seeded-b"])
        self.assertIn("two clusters", text)

    def test_an_italic_question_keeps_its_buttons_and_loses_its_underscores(self):
        blocks, text = m.needs_you("_Which cluster should I drain?_\n- seeded-a\n- seeded-b")
        self.assertEqual(blocks[0]["text"]["text"], "*Which cluster should I drain?*")
        self.assertEqual([b["value"] for b in _buttons(blocks)], ["seeded-a", "seeded-b"])
        self.assertNotIn("_", text)
        blocks, _ = m.needs_you("Which pool?\n- _gpu-pool_\n- node_pool")
        self.assertEqual([b["value"] for b in _buttons(blocks)], ["gpu-pool", "node_pool"])

    def test_a_fenced_block_before_the_question_stays_in_the_detail(self):
        table = "```\nNAME   READY\nweb-1  0/1\n```"
        blocks, _ = m.needs_you(f"{table}\nWhich pod should I restart?\n- web-1\n- web-2")
        self.assertEqual(blocks[0]["text"]["text"], "*Which pod should I restart?*")
        self.assertEqual([b["value"] for b in _buttons(blocks)], ["web-1", "web-2"])
        self.assertEqual(_contexts(blocks), [table, m.WAITING])

    def test_the_detail_fits_slacks_limit_once_escaped(self):
        blocks, _ = m.needs_you("Question\n" + "<a> " * 400)
        detail = _contexts(blocks)[0]
        self.assertLessEqual(len(detail), m.DETAIL_MAX)
        self.assertTrue(detail.endswith("…"))
        self.assertIn("&lt;a&gt;", detail)

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

    def test_a_headline_with_a_link_keeps_the_url_on_the_line_below(self):
        url = "https://example.com/" + "a" * 150
        blocks, _ = m.needs_you(f"Check the [runbook]({url}) first. Which?\n- seeded-a\n- seeded-b")
        self.assertIn("Check the runbook first. Which?", blocks[0]["text"]["text"])
        self.assertEqual(_contexts(blocks)[0], f"Check the <{url}|runbook> first. Which?")
        self.assertEqual([b["value"] for b in _buttons(blocks)], ["seeded-a", "seeded-b"])

    def test_a_short_headline_with_a_link_keeps_the_url_on_the_line_below(self):
        url = "https://github.com/acme/x/pull/412"
        blocks, _ = m.needs_you(f"Should I merge [PR #412]({url})?\n- Yes\n- No")
        self.assertEqual(_contexts(blocks)[0], f"Should I merge <{url}|PR #412>?")
        self.assertEqual([b["value"] for b in _buttons(blocks)], ["Yes", "No"])

    def test_a_short_headline_without_a_link_is_not_repeated_below(self):
        blocks, _ = m.needs_you("Which cluster?\n- seeded-a\n- seeded-b")
        self.assertEqual([b for b in blocks if b["type"] == "context" and b.get("block_id") != p.WAITING_BLOCK_ID], [])

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
