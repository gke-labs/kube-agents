"""scrub_bot_posts.py: the plan over the bots' older posts (the shapes the
templates rendered before 2026-11, taken from the templates at 9846f05c9),
its idempotence on the current shapes, and apply's dry-run and live paths.

Run from scripts/: python3 -m unittest test_eval_dashboard_scrub_bot_posts
"""

import io
import json
import re
import unittest
import unittest.mock

from eval_dashboard import coverage_gaps, gate_comment, gate_issue, ghcli, nightly_bugs, scrub_bot_posts
from eval_dashboard.post_health import NIGHTLY_URL

# What a public post must never carry; the same guard as the bots' tests.
RESULTS_RE = re.compile(r"\d+/\d+|\d+ of \d+|\d+ repetitions?|\d+ ?%|\bpassed\b|failed every|failed all|fails every|nights in a row|RESOURCE_EXHAUSTED|\b429\b")
BOT = {"login": "github-actions[bot]"}
HUMAN = {"login": "alice"}
SPYGLASS = "https://oss.gprow.dev/view/gs/kube-agents-evals-nightly-logs/logs/ci-kube-agents-eval-nightly"
CONSOLE = "https://console.cloud.google.com/storage/browser/kube-agents-evals-nightly-logs/logs/ci-kube-agents-eval-nightly"
OBJECT = "https://storage.cloud.google.com/kube-agents-evals-nightly-logs/logs/ci-kube-agents-eval-nightly"
LEGACY = "https://oss.gprow.dev/view/gs/kube-agents-prow/logs/ci-kube-agents-eval-nightly"
URL = "https://github.com/gke-labs/kube-agents/issues/{n}"

OLD_RED_BODY = f"""\
<!-- kube-agents-bot:nightly-red case=case-red -->
`case-red` failed every graded repetition on its last 3 graded nightly nights. Domain: **Capacity** (`capacity`).

**Assigned to** @alice (the case's `owner:`). Domain owner: @carol, backup @dave.

**Nights** (newest first; a night cut off at the job deadline, a night where nothing was graded, and a night where nearly every case failed are skipped, not counted)

- 2026-10-17: 0/3 passed · [run]({SPYGLASS}/30000000000000170001) · [transcript]({SPYGLASS}/30000000000000170001/artifacts/eval_case-red_rep1.log)
  - failed: VerificationCorrectness=0.0 (floor 1.0) -- names-the-pool: required phrases absent
- 2026-10-16: 0/3 passed (1 infra) · [run]({LEGACY}/30000000000000160001) · [transcript]({LEGACY}/30000000000000160001/artifacts/eval_case-red_rep1.log)
  - failed: agent run failed: http 429 RESOURCE_EXHAUSTED
- 2026-10-15: 0/2 passed

Nightly report: {NIGHTLY_URL}

**What triage decides (within a week)**

- **Product bug:** name a fixer, a priority and a target date.
- **Broken test:** fix the case, or mark it `expected_fail: true` in its `task.yaml` with this issue linked.
- **Infra:** close this issue and point to the infra issue.

Filed automatically by `scripts/eval_dashboard/nightly_bugs.py` (docs/ci-health.md, "Nightly bugs"). It comments here when the case passes every repetition again; it never closes the issue.
"""
OLD_INFRA_BODY = f"""\
<!-- kube-agents-bot:nightly-infra case=case-red -->
`case-red` graded nothing on its last 3 nights where its part of the nightly (`ci-kube-agents-eval-nightly`) ran to its verdict: every repetition was classified as infrastructure (`classify.py`), or the run recorded no line for the case. Domain: `capacity` (owner @carol).

**Assigned to** @ivan (the infra owner in `docs/designs/domains.yaml`). Infra backup: @iris.

**Nights** (newest first; a night the part did not run or lost, and a night infrastructure took half the part's cases, are skipped)

- 2026-10-17: `3 of 3 repetitions infrastructure: agent run failed: http 429 RESOURCE_EXHAUSTED` · [run]({SPYGLASS}/30000000000000170001) · [transcript]({SPYGLASS}/30000000000000170001/artifacts/eval_case-red_rep1.log)
- 2026-10-16: `no line for the case: the main part ran to its verdict and recorded nothing for it` · [run]({SPYGLASS}/30000000000000160001)

Nightly report: {NIGHTLY_URL}

{nightly_bugs.INFRA_TRIAGE}"""
OLD_LOST_BODY = f"""\
<!-- kube-agents-bot:nightly-lost job=ci-kube-agents-eval-nightly -->
The nightly's main part (`ci-kube-agents-eval-nightly`) lost 2 of the last 3 nightly dates: Prow ended the build before the eval loop's verdict (the job deadline, an abort) or the pod went away, so the cases it had not reached were never graded.

**Assigned to** @ivan (the infra owner in `docs/designs/domains.yaml`).

**Nights** (newest first)

- 2026-10-17: the pod was lost (node node-7, last event NodeNotReady); nothing recorded · [run]({SPYGLASS}/30000000000000170001)
- 2026-10-16: ran to its verdict (result SUCCESS); 5 cases recorded · [run]({SPYGLASS}/30000000000000160001)

Nightly report: {NIGHTLY_URL}

{nightly_bugs.INFRA_TRIAGE}"""
OLD_COVERAGE_BODY = """\
<!-- kube-agents-bot:coverage-gap domain=beta -->
**Beta** (`beta`) has no case on `hack/eval/blocking-roster.txt` for 9 days (since 2026-10-17), so no pull request can go red on it. The domain: beta things.

**Assigned to** @carol (the domain owner in `docs/designs/domains.yaml`). Backup: @dave.

**Why it is uncovered:** on the `allowlist` in `docs/designs/domains.yaml` for #3120 since 2026-10-17; the comment above the entry carries the full reason and its exit condition.

**Candidates** (the domain's cases on the nightly matrix with a `verification_spec`, not `expected_fail`, best first). The nightly record is the graded repetitions over the last 14 nightly nights, read as the nightly bug filer reads them; **Bar** holds it to the roster's 90% over at least 20 graded runs, and the evidence store applies the same bar at the current next-mode version key.

| Case | Nightly record | Bar | Evidence store |
|---|---|---|---|
| `beta-good` | 21/21 over 7 nights (100%) | meets 90% | not read this tick |
| `beta-held` (held out on the presubmit) | 18/21 over 7 nights (85%) | below 90% | collecting, 3/5, 17 more runs for a full window |

Nightly report: https://storage.cloud.google.com/kube-agents-dashboards/evals/nightly.html

**What the owner decides** (one pull request, approved by the eval crew)
"""
OLD_OUTAGE_TITLE = "Smoke gate outage: 2 cases failing on every PR since Tue 7:30 AM ET"
OLD_OUTAGE_BODY = """\
The smoke gate (`pull-kube-agents-smoke-test`) is in OUTAGE: the cases below fail every repetition on every pull request that runs them, so a red on an open PR is not that PR's code.

**Failing cases**

- `reliability-pdb-probe`
- `cost-idle-pool-probe`

**Window:** since Tue 7:30 AM ET (2026-09-08T11:30:00+00:00), 3 PRs red so far.
**Class:** shared fixture/environment break: cost-idle-pool-probe, reliability-pdb-probe (`shared_break`).
**Evidence:**

- cost-idle-pool-probe failed all graded reps on 4 runs from 3 PRs (#1, #2, #3)
- these cases explain 4 of 5 red runs (5 of 6 concluded runs red) in the last 6h

Incident brief: https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html#since=2026-09-08T11:30:00Z&cases=cost-idle-pool-probe,reliability-pdb-probe&view=gate

Filed automatically by the smoke health bot; edit freely. Fix PRs: reference this issue.
"""
LOST_PODS_TITLE = "Build cluster lost node(s) gke-kube-agents-prow-default-pool-eb220b2a-er33 at Fri 10:05 AM ET: 12 smoke runs on 12 PRs died mid-run"
LOST_PODS_BODY = """\
The Prow build cluster (`kube-agents-prow`) lost the node(s) below at Fri 10:05 AM ET; 12 `pull-kube-agents-smoke-test` runs on 12 pull requests died mid-run.

**Nodes**

- `gke-kube-agents-prow-default-pool-eb220b2a-er33` (12 runs)

**Window:** 10:05–10:19 AM ET (2026-09-11T14:05:52+00:00 – 2026-09-11T14:19:16+00:00).
**Affected PRs:** #1, #2.
**Evidence:**

- cost-idle-pool-probe failed all graded reps on 4 runs from 3 PRs (#1, #2, #3)
- lost pods: 12 runs on 12 PRs died with their build node 14:05–14:19 UTC (nodes gke-kube-agents-prow-default-pool-eb220b2a-er33; #1, #2)
- PR-caused: upgrades-lagging-master-probe failing only on #7 (passing on 2 other PRs built on the same main commit)

**Advice for authors:** nothing about your change; `/retest` once new jobs are progressing.

Incident brief: brief
"""
FIXTURE_TITLE = "Seeded fleet drift: crashloop-workload out of designed state on 3 pool projects since Mon 9:00 AM ET"
FIXTURE_BODY = """\
The seeded fleet's fixture role(s) below are present but not in the state the cases depend on.

**Drifted roles**

- `crashloop-workload`

**Window:** since Mon 9:00 AM ET (2026-09-14T13:00:00+00:00); latest scan 2026-09-14T13:00:00+00:00.
**Evidence:**

- crashloop-workload drifted on 3 pool project(s) (kube-agents-evals-1, kube-agents-evals-2, kube-agents-evals-3) at the 13:00 UTC scan
- certification lane: build 2100000000000000001 failed at 2026-11-07T03:10:00Z (log `ERROR rca-remediation-pr: required phrases absent`)

**Reconcile:** the daily run re-applies the stack.
"""


def issue(number, title, body, labels=("nightly-red",), user=BOT, pull_request=False):
    doc = {"number": number, "html_url": URL.format(n=number), "title": title, "body": body, "labels": [{"name": name} for name in labels], "user": user}
    if pull_request:
        doc["pull_request"] = {"url": "x"}
    return doc


def comment(cid, body, user=BOT, on=1):
    return {"id": cid, "html_url": f"https://github.com/gke-labs/kube-agents/issues/{on}#issuecomment-{cid}", "body": body, "user": user,
            "issue_url": f"https://api.github.com/repos/gke-labs/kube-agents/issues/{on}"}


# The smoke-gate comment as gate_comment.py rendered it before the change
# (the heading's count, the per-case table, the Reason line, the footer's
# count) and as it renders it now.
OLD_GATE_COMMENT = f"""\
{gate_comment.MARKER}
### ❌ Smoke gate: failed · 1 of 18 cases

> 🟢 **Gate healthy.** `upgrades-lagging-master-probe` failed here and nothing on other PRs matches it yet; read the transcript. [Why this run failed →](https://storage.cloud.google.com/kube-agents-dashboards/evals/run.html#build=2098525469841297408)

| Case | Result | Also failing on |
| --- | --- | --- |
| `upgrades-lagging-master-probe` | 0 / 3 reps | no other PR |

Reason: `the-probe-identifies-the-version-lag: none of the alternative phrasings present`

16 cases passed. Run 145 min on evals-23 · [build log](https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/1488/pull-kube-agents-smoke-test/2098525469841297408)
"""
CURRENT_GATE_COMMENT = f"""\
{gate_comment.MARKER}
### ❌ Smoke gate: failed

> 🟢 **Gate healthy.** Part of what failed here matches nothing on other PRs yet; read the transcript. [Why this run failed →](https://storage.cloud.google.com/kube-agents-dashboards/evals/run.html#build=2098525469841297408)

Run 145 min · [build log](https://console.cloud.google.com/storage/browser/kube-agents-evals-presubmit-logs/pr-logs/pull/gke-labs_kube-agents/1488/pull-kube-agents-smoke-test/2098525469841297408)
"""


# The one percentage a coverage body may print is the roster's bar, a
# constant, not a record (coverage_gaps' own guard test reads past it too).
BAR_SENTENCE_RE = re.compile(r"held to the roster's bar of \d+% over at least \d+ graded runs")


def clean(text):
    return text is not None and RESULTS_RE.search(BAR_SENTENCE_RE.sub("", text)) is None


class ConsoleLinkTest(unittest.TestCase):
    def test_private_spyglass_links_become_console_links_and_others_stay(self):
        self.assertEqual(scrub_bot_posts.console_link(f"{SPYGLASS}/300"), f"{CONSOLE}/300")
        self.assertEqual(scrub_bot_posts.console_link(f"{SPYGLASS}/300/artifacts/eval_case-b_rep1.log"), f"{OBJECT}/300/artifacts/eval_case-b_rep1.log")
        self.assertEqual(scrub_bot_posts.console_link(f"{LEGACY}/300"), f"{LEGACY}/300", "a bucket Deck reads keeps Spyglass")
        self.assertEqual(scrub_bot_posts.console_link(f"{CONSOLE}/300"), f"{CONSOLE}/300", "already a Console link")
        self.assertIsNone(scrub_bot_posts.console_link(None))


class GateCommentTest(unittest.TestCase):
    def test_the_bots_old_gate_comment_on_a_closed_pull_request_is_deleted_and_nothing_else_is(self):
        own = comment(7, OLD_GATE_COMMENT, on=1488)
        action = scrub_bot_posts.plan_comment(own, frozenset({5004}))
        self.assertEqual((action.verb, action.path), ("delete", "issues/comments/7"))
        self.assertEqual(scrub_bot_posts.plan_comment(comment(8, OLD_GATE_COMMENT, HUMAN)).verb, "skip")
        self.assertEqual(scrub_bot_posts.plan_comment(comment(9, "LGTM")).verb, "skip")
        self.assertEqual(scrub_bot_posts.plan_comment(comment(9, "LGTM")).kind, scrub_bot_posts.KIND_OTHER)

    def test_a_current_shape_comment_is_left_on_a_closed_pull_request(self):
        action = scrub_bot_posts.plan_comment(comment(10, CURRENT_GATE_COMMENT, on=1488), frozenset())
        self.assertEqual((action.verb, action.reason), ("skip", scrub_bot_posts.REASON_CURRENT))
        # Every current wording template in gate_comment (the string constants with a
        # space in them: the one-word ones are classify/health aliases and paths), its
        # placeholders filled with a neutral word, reads as current, so the check follows
        # the code; the old shapes do not.
        class Neutral(dict):
            def __missing__(self, key):
                return "x"

        templates = {name: value for name, value in vars(gate_comment).items() if name.isupper() and isinstance(value, str) and " " in value}
        self.assertGreater(len(templates), 20)
        for name, template in templates.items():
            self.assertIsNone(scrub_bot_posts.OLD_GATE_COMMENT_RE.search(template.format_map(Neutral())), name)
        for old in ("### ❌ Smoke gate: failed · 1 of 18 cases", "| Case | Result | Also failing on |", "| `x` | 0 / 3 reps | no other PR |", "Reason: `x`", "16 cases passed.",
                    "`case-a`, `case-b` lost every repetition to infrastructure before the agent could be graded.",
                    "`case-x` also failed every graded repetition here; read its transcript before retesting.",
                    "**The run failed without a gate case failing all of its repetitions.**"):
            self.assertIsNotNone(scrub_bot_posts.OLD_GATE_COMMENT_RE.search(old), old)

    def test_a_comment_on_an_open_pull_request_is_left_whatever_its_shape(self):
        action = scrub_bot_posts.plan_comment(comment(11, OLD_GATE_COMMENT, on=5004), frozenset({5004}))
        self.assertEqual((action.verb, action.reason), ("skip", scrub_bot_posts.REASON_OPEN_PR))


class NightlyIssueTest(unittest.TestCase):
    def test_an_old_red_issue_is_rewritten_to_the_current_shape(self):
        old = issue(3990, "Nightly: case-red fails every run, 3 nights in a row", OLD_RED_BODY)
        action = scrub_bot_posts.plan_issue(old)
        self.assertEqual((action.verb, action.kind, action.path), ("edit", scrub_bot_posts.KIND_NIGHTLY_RED, "issues/3990"))
        self.assertEqual(action.title, "Nightly: case-red needs triage")
        body = action.body
        self.assertTrue(body.startswith("<!-- kube-agents-bot:nightly-red case=case-red -->\n`case-red` needs triage"))
        self.assertIn("Domain: **Capacity** (`capacity`).", body)
        self.assertIn("**Assigned to** @alice (the case's `owner:`). Domain owner: @carol, backup @dave.", body)
        self.assertIn(f"- 2026-10-17 · [run]({CONSOLE}/30000000000000170001) · [transcript]({OBJECT}/30000000000000170001/artifacts/eval_case-red_rep1.log)\n"
                      f"- 2026-10-16 · [run]({LEGACY}/30000000000000160001) · [transcript]({LEGACY}/30000000000000160001/artifacts/eval_case-red_rep1.log)\n"
                      "- 2026-10-15\n", body, "the dates stay, the counts and reasons go, private links go to the Console")
        self.assertNotIn("names-the-pool", body)
        self.assertTrue(clean(action.title) and clean(body), body)
        # Idempotent: the rewritten issue plans as a skip.
        again = scrub_bot_posts.plan_issue(issue(3990, action.title, body))
        self.assertEqual((again.verb, again.reason), ("skip", scrub_bot_posts.REASON_CURRENT))

    def test_an_old_red_issue_the_body_of_which_cannot_be_read_is_skipped(self):
        action = scrub_bot_posts.plan_issue(issue(3991, "t", "<!-- kube-agents-bot:nightly-red case=case-red -->\nedited by hand"))
        self.assertEqual(action.verb, "skip")
        self.assertIn("could not read", action.reason)

    def test_an_old_kept_case_issue_is_rewritten(self):
        action = scrub_bot_posts.plan_issue(issue(3971, "Nightly infra: case-red kept from running 3 nights in a row", OLD_INFRA_BODY))
        self.assertEqual((action.verb, action.kind, action.title), ("edit", scrub_bot_posts.KIND_NIGHTLY_INFRA, "Nightly infra: case-red kept from running"))
        body = action.body
        self.assertIn("(`ci-kube-agents-eval-nightly`) ran to its verdict", body)
        self.assertIn("Domain: `capacity` (owner @carol).", body)
        self.assertIn("**Assigned to** @ivan (the infra owner in `docs/designs/domains.yaml`). Infra backup: @iris.", body)
        self.assertIn(f"- 2026-10-17 · [run]({CONSOLE}/30000000000000170001) · [transcript]({OBJECT}/30000000000000170001/artifacts/eval_case-red_rep1.log)\n"
                      f"- 2026-10-16 · [run]({CONSOLE}/30000000000000160001)\n", body)
        self.assertNotIn("repetitions infrastructure", body)
        self.assertTrue(clean(body), body)
        again = scrub_bot_posts.plan_issue(issue(3971, action.title, body))
        self.assertEqual(again.verb, "skip")

    def test_a_lost_nights_issue_only_has_its_links_converted(self):
        action = scrub_bot_posts.plan_issue(issue(3960, "Nightly infra: ci-kube-agents-eval-nightly lost 2 of the last 3 nightly dates", OLD_LOST_BODY))
        self.assertEqual((action.verb, action.kind, action.title), ("edit", scrub_bot_posts.KIND_NIGHTLY_LOST, None))
        self.assertIn(f"- 2026-10-17: the pod was lost (node node-7, last event NodeNotReady); nothing recorded · [run]({CONSOLE}/30000000000000170001)", action.body)
        self.assertTrue(clean(action.body), action.body)
        self.assertEqual(scrub_bot_posts.plan_issue(issue(3960, "t", action.body)).verb, "skip")

    def test_a_pull_request_and_a_human_issue_are_skipped(self):
        self.assertEqual(scrub_bot_posts.plan_issue(issue(1, "t", OLD_RED_BODY, pull_request=True)).reason, scrub_bot_posts.REASON_PULL_REQUEST)
        action = scrub_bot_posts.plan_issue(issue(2, "case-red fails on main", "see the nightly"))
        self.assertEqual((action.verb, action.kind), ("skip", scrub_bot_posts.KIND_OTHER))
        # A human's issue carrying a copied marker, or an infra-shaped title, is never rewritten.
        copied = scrub_bot_posts.plan_issue(issue(3, "Nightly: case-red fails every run, 3 nights in a row", OLD_RED_BODY, user=HUMAN))
        self.assertEqual((copied.verb, copied.kind, copied.reason), ("skip", scrub_bot_posts.KIND_NIGHTLY_RED, scrub_bot_posts.REASON_NOT_BOT))
        shaped = scrub_bot_posts.plan_issue(issue(4, LOST_PODS_TITLE, LOST_PODS_BODY, labels=("presubmit-gate",), user=HUMAN))
        self.assertEqual((shaped.verb, shaped.reason), ("skip", scrub_bot_posts.REASON_NOT_BOT))


class NightlyCommentTest(unittest.TestCase):
    def test_every_bot_comment_on_a_nightly_issue_is_deleted(self):
        comments = [
            comment(70, f"<!-- kube-agents-bot:nightly-red-recovered case=case-red night=2026-10-18 -->\n`case-red` passed every graded repetition (3/3) on the 2026-10-18 nightly: [run]({SPYGLASS}/30000000000000180001). The bot will not close this issue; the triager decides whether it is fixed.\n"),
            comment(71, f"<!-- kube-agents-bot:nightly-infra-night case=case-red night=2026-10-18 -->\nAgain on the 2026-10-18 nightly: `3 of 3 repetitions infrastructure: http 429 RESOURCE_EXHAUSTED` · [run]({SPYGLASS}/30000000000000180001).\n"),
            comment(72, f"<!-- kube-agents-bot:nightly-lost-night job=ci-kube-agents-eval-nightly night=2026-10-18 -->\nAgain on the 2026-10-18 nightly: ended before its verdict after 6h (result ABORTED); 3 cases recorded · [run]({SPYGLASS}/30000000000000180001).\n"),
            comment(73, f"<!-- kube-agents-bot:nightly-infra-recovered case=case-red night=2026-10-19 -->\n`case-red` was graded on the 2026-10-19 nightly: [run]({LEGACY}/30000000000000190001). The bot will not close this issue; the triager decides whether it is fixed.\n"),
            comment(74, "<!-- kube-agents-bot:nightly-lost-recovered job=ci-kube-agents-eval-nightly night=2026-10-19 -->\nThe main part ran to its verdict on the 2026-10-19 nightly.\n"),
        ]
        for c in comments:
            action = scrub_bot_posts.plan_issue_comment(c)
            self.assertEqual((action.verb, action.kind, action.path, action.reason), ("delete", scrub_bot_posts.KIND_NIGHTLY_COMMENT, f"issues/comments/{c['id']}", scrub_bot_posts.REASON_BOT_COMMENT))

    def test_a_humans_comment_is_left_even_with_a_copied_marker(self):
        marker = "<!-- kube-agents-bot:nightly-red-recovered case=case-red night=2026-10-18 -->"
        self.assertEqual(scrub_bot_posts.plan_issue_comment(comment(75, marker + "\nquoted", HUMAN)).verb, "skip")
        self.assertEqual(scrub_bot_posts.plan_issue_comment(comment(76, "a human's remark")).kind, scrub_bot_posts.KIND_OTHER)


class CoverageIssueTest(unittest.TestCase):
    def test_the_table_is_reduced_to_its_first_column_and_the_rest_kept(self):
        action = scrub_bot_posts.plan_issue(issue(4100, "Presubmit coverage: beta has no blocking roster seat", OLD_COVERAGE_BODY, labels=("presubmit-gate", "domain:beta")))
        self.assertEqual((action.verb, action.kind, action.title), ("edit", scrub_bot_posts.KIND_COVERAGE, None))
        self.assertIn(coverage_gaps.TABLE_HEAD + "\n| `beta-good` |\n| `beta-held` (held out on the presubmit) |\n\nNightly report:", action.body)
        self.assertNotIn("21/21", action.body)
        self.assertIn("**Candidates** (the domain's cases on the nightly matrix with a `verification_spec`, not `expected_fail`). They are ranked by their nightly record over the last 14 nightly nights", action.body)
        self.assertIn("[domain scorecard](https://storage.cloud.google.com/kube-agents-dashboards/evals/cases.html#view=domains)", action.body)
        self.assertNotIn("**Bar** holds it", action.body)
        self.assertTrue(clean(action.body), action.body)
        self.assertIn("**Assigned to** @carol", action.body)
        self.assertEqual(scrub_bot_posts.plan_issue(issue(4100, "t", action.body, labels=("presubmit-gate",))).verb, "skip")


class OutageIssueTest(unittest.TestCase):
    def test_the_old_outage_issue_is_retitled_and_its_body_rewritten_with_the_marker(self):
        action = scrub_bot_posts.plan_issue(issue(1300, OLD_OUTAGE_TITLE, OLD_OUTAGE_BODY, labels=("presubmit-gate",)))
        self.assertEqual((action.verb, action.kind, action.title), ("edit", scrub_bot_posts.KIND_OUTAGE, "Smoke gate outage since Tue 7:30 AM ET"))
        body = action.body
        self.assertTrue(body.startswith("<!-- kube-agents-bot:gate-outage cases=cost-idle-pool-probe,reliability-pdb-probe -->\n"))
        self.assertIn("**Window:** since Tue 7:30 AM ET (2026-09-08T11:30:00+00:00), 3 PRs red so far.", body)
        self.assertIn("**Class:** shared fixture/environment break (`shared_break`).", body)
        self.assertIn("The failing cases and the evidence are on the incident brief: https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html#since=2026-09-08T11:30:00Z&view=gate", body)
        rest = body.split("\n", 1)[1]
        self.assertNotIn("cost-idle-pool-probe", rest)
        self.assertTrue(clean(action.title) and clean(body), body)
        self.assertEqual(scrub_bot_posts.plan_issue(issue(1300, action.title, body, labels=("presubmit-gate",))).reason, scrub_bot_posts.REASON_CURRENT)

    def test_an_outage_issue_without_its_window_line_is_skipped(self):
        action = scrub_bot_posts.plan_issue(issue(1301, OLD_OUTAGE_TITLE, "rewritten by a human", labels=("presubmit-gate",)))
        self.assertEqual(action.verb, "skip")
        self.assertIn("could not read", action.reason)


class InfrastructureIssueTest(unittest.TestCase):
    def test_the_lost_pods_and_fixture_drift_bodies_keep_only_their_own_evidence(self):
        action = scrub_bot_posts.plan_issue(issue(1400, LOST_PODS_TITLE, LOST_PODS_BODY, labels=("presubmit-gate",)))
        self.assertEqual((action.verb, action.kind), ("edit", scrub_bot_posts.KIND_INFRA_ISSUE))
        self.assertIn("**Evidence:**\n\n- lost pods: 12 runs on 12 PRs died with their build node 14:05–14:19 UTC (nodes gke-kube-agents-prow-default-pool-eb220b2a-er33; #1, #2)\n\n**Advice", action.body)
        self.assertNotIn("cost-idle-pool-probe", action.body)
        self.assertNotIn("PR-caused", action.body)
        self.assertTrue(clean(action.body), action.body)
        self.assertEqual(scrub_bot_posts.plan_issue(issue(1400, LOST_PODS_TITLE, action.body, labels=("presubmit-gate",))).verb, "skip")
        action = scrub_bot_posts.plan_issue(issue(1401, FIXTURE_TITLE, FIXTURE_BODY, labels=("presubmit-gate",)))
        self.assertEqual(action.verb, "edit")
        self.assertIn("**Evidence:**\n\n- crashloop-workload drifted on 3 pool project(s)", action.body)
        self.assertNotIn("certification lane", action.body)
        self.assertNotIn("rca-remediation-pr", action.body)
        self.assertTrue(clean(action.body), action.body)

    def test_an_evidence_block_with_nothing_to_drop_is_left(self):
        body = LOST_PODS_BODY.replace("- cost-idle-pool-probe failed all graded reps on 4 runs from 3 PRs (#1, #2, #3)\n", "").replace("- PR-caused: upgrades-lagging-master-probe failing only on #7 (passing on 2 other PRs built on the same main commit)\n", "")
        self.assertEqual(scrub_bot_posts.plan_issue(issue(1402, LOST_PODS_TITLE, body, labels=("presubmit-gate",))).verb, "skip")


class Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class FakeRunner:
    """`gh api` as scrub_bot_posts reads it: the repository comments, the
    labelled issues and each issue's comments; every call recorded."""

    def __init__(self, comments=(), issues=(), issue_comments=None, open_prs=()):
        self.comments, self.issues, self.issue_comments = list(comments), list(issues), issue_comments or {}
        self.open_prs = list(open_prs)
        self.calls = []

    def __call__(self, argv, input=None, **kwargs):
        method, path = argv[argv.index("-X") + 1], argv[argv.index("-X") + 2]
        self.calls.append((method, path, json.loads(input) if input else None))
        if method != "GET":
            return Proc("{}")
        page = int(path.rsplit("&page=", 1)[1])
        if "/issues/comments?" in path:
            items = self.comments
        elif re.search(r"/issues/\d+/comments", path):
            number = int(path.split("/issues/")[1].split("/")[0])
            items = self.issue_comments.get(number, [])
        elif "/issues?" in path:
            label = path.split("labels=")[1].split("&")[0]
            items = [i for i in self.issues if label in {lb["name"] for lb in i["labels"]}]
        elif "/pulls?" in path:
            items = [{"number": n} for n in self.open_prs]
        else:
            items = []
        start = (page - 1) * scrub_bot_posts.PAGE_SIZE
        return Proc(json.dumps(items[start:start + scrub_bot_posts.PAGE_SIZE]))

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


class ReadListTest(unittest.TestCase):
    def test_a_listing_is_read_a_page_per_call_until_a_short_page(self):
        many = [comment(i, "x") for i in range(scrub_bot_posts.PAGE_SIZE * 2 + 1)]
        runner = FakeRunner(comments=many)
        gh = ghcli.Gh("gke-labs/kube-agents", runner, dry_run=True)
        out = scrub_bot_posts.read_list(gh, scrub_bot_posts.COMMENTS_SINCE_PATH.format(since="2026-09-01T00:00:00Z"))
        self.assertEqual(len(out), len(many))
        self.assertEqual([p.rsplit("&page=", 1)[1] for _, p, _ in runner.calls], ["1", "2", "3"])

    def test_a_page_that_fails_ends_the_listing(self):
        class Failing(FakeRunner):
            def __call__(self, argv, input=None, **kwargs):
                proc = super().__call__(argv, input, **kwargs)
                return Proc("", returncode=1, stderr="HTTP 502") if argv[-1].endswith("&page=2") else proc
        runner = Failing(comments=[comment(i, "x") for i in range(scrub_bot_posts.PAGE_SIZE + 5)])
        gh = ghcli.Gh("gke-labs/kube-agents", runner, dry_run=True)
        self.assertEqual(len(scrub_bot_posts.read_list(gh, scrub_bot_posts.COMMENTS_SINCE_PATH.format(since="s"))), scrub_bot_posts.PAGE_SIZE)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner(
            comments=[comment(7, OLD_GATE_COMMENT, on=1488), comment(8, "a human's"), comment(9, OLD_GATE_COMMENT, on=5004), comment(10, CURRENT_GATE_COMMENT, on=1500)],
            open_prs=[5004],
            issues=[issue(3990, "Nightly: case-red fails every run, 3 nights in a row", OLD_RED_BODY), issue(1300, OLD_OUTAGE_TITLE, OLD_OUTAGE_BODY, labels=("presubmit-gate",))],
            issue_comments={3990: [comment(70, "<!-- kube-agents-bot:nightly-red-recovered case=case-red night=2026-10-18 -->\n`case-red` passed every graded repetition (3/3) on the 2026-10-18 nightly.")]},
        )

    def run_main(self, *args):
        out = io.StringIO()
        with unittest.mock.patch.object(scrub_bot_posts.time, "sleep") as sleep, unittest.mock.patch("sys.stdout", out):
            rc = scrub_bot_posts.main(list(args), gh_runner=self.runner)
        self.sleep = sleep
        return rc, out.getvalue()

    def test_dry_run_prints_every_action_and_writes_nothing(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(self.runner.writes(), [])
        self.assertIn("would delete smoke-gate comment https://github.com/gke-labs/kube-agents/issues/1488#issuecomment-7", out)
        self.assertIn(f"skip smoke-gate comment https://github.com/gke-labs/kube-agents/issues/5004#issuecomment-9: {scrub_bot_posts.REASON_OPEN_PR}", out)
        self.assertIn(f"skip smoke-gate comment https://github.com/gke-labs/kube-agents/issues/1500#issuecomment-10: {scrub_bot_posts.REASON_CURRENT}", out)
        self.assertIn(f"    1  smoke-gate comment: skip ({scrub_bot_posts.REASON_OPEN_PR})\n", out)
        self.assertIn(f"    1  smoke-gate comment: skip ({scrub_bot_posts.REASON_CURRENT})\n", out)
        self.assertIn("    1  smoke-gate comment: delete\n", out)
        self.assertIn("would edit nightly-red issue https://github.com/gke-labs/kube-agents/issues/3990\n  title: Nightly: case-red needs triage", out)
        self.assertIn("would edit outage issue", out)
        self.assertIn("would delete nightly issue comment", out)
        self.assertIn(scrub_bot_posts.SUMMARY_HEADING + "\n  https://github.com/gke-labs/kube-agents/issues/3990\n  https://github.com/gke-labs/kube-agents/issues/1300", out)
        self.assertNotIn("a human's", out, "human posts are not listed")

    def test_live_performs_the_writes_paced_and_up_to_the_limit(self):
        rc, out = self.run_main("--live", "--pace", "0.5", "--limit", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(self.runner.writes(), [("DELETE", "repos/gke-labs/kube-agents/issues/comments/7"), ("PATCH", "repos/gke-labs/kube-agents/issues/3990")])
        patched = next(body for m, p, body in self.runner.calls if m == "PATCH")
        self.assertEqual(patched["title"], "Nightly: case-red needs triage")
        self.assertIn("limit 2 reached", out)
        self.assertEqual(self.sleep.call_args_list, [unittest.mock.call(0.5)], "one pause, between the two writes")

    def test_the_limit_is_checked_before_a_live_write_is_announced(self):
        self.runner = FakeRunner(comments=[comment(i, OLD_GATE_COMMENT) for i in range(4)])
        rc, out = self.run_main("--live", "--pace", "0", "--limit", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runner.writes()), 2)
        before_limit = out.split("limit 2 reached; the rest is left for the next run\n")[0]
        self.assertEqual([line for line in before_limit.splitlines() if line.startswith("delete ")],
                         ["delete smoke-gate comment https://github.com/gke-labs/kube-agents/issues/1#issuecomment-0",
                          "delete smoke-gate comment https://github.com/gke-labs/kube-agents/issues/1#issuecomment-1"],
                         "exactly the two writes sent are announced; the third is not claimed")
        self.assertIn("    2  smoke-gate comment: delete", out)

    def test_a_failed_write_is_counted_as_failed_and_three_in_a_row_stop_the_run(self):
        class Refusing(FakeRunner):
            def __call__(self, argv, input=None, **kwargs):
                proc = super().__call__(argv, input, **kwargs)
                return Proc("", returncode=1, stderr="HTTP 403") if argv[argv.index("-X") + 1] != "GET" else proc
        many = [comment(i, OLD_GATE_COMMENT) for i in range(10)]
        self.runner = Refusing(comments=many, issues=[issue(1300, OLD_OUTAGE_TITLE, OLD_OUTAGE_BODY, labels=("presubmit-gate",))])
        rc, out = self.run_main("--live", "--pace", "0")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runner.writes()), scrub_bot_posts.MAX_CONSECUTIVE_FAILURES, "stopped after three failures in a row")
        self.assertIn("3 writes in a row failed", out)
        self.assertIn("    3  smoke-gate comment: failed", out)
        self.assertNotIn(scrub_bot_posts.SUMMARY_HEADING, out, "no issue was edited, so none to list")

    def test_failures_with_a_success_between_them_do_not_stop_the_run(self):
        class Alternating(FakeRunner):
            def __call__(self, argv, input=None, **kwargs):
                proc = super().__call__(argv, input, **kwargs)
                if argv[argv.index("-X") + 1] == "GET":
                    return proc
                return Proc("", returncode=1, stderr="HTTP 502") if int(argv[-1].rsplit("/", 1)[1]) % 2 else proc
        many = [comment(i, OLD_GATE_COMMENT) for i in range(10)]
        self.runner = Alternating(comments=many)
        rc, out = self.run_main("--live", "--pace", "0")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runner.writes()), 10, "every write attempted: no three failed in a row")
        self.assertIn("    5  smoke-gate comment: failed", out)
        self.assertIn("    5  smoke-gate comment: delete", out)
        self.assertNotIn("in a row failed", out)

    def test_a_dry_run_delete_never_reaches_gh(self):
        gh = ghcli.Gh("gke-labs/kube-agents", self.runner, dry_run=True)
        gh.call("DELETE", gh.path("issues/comments/7"))
        self.assertEqual(self.runner.writes(), [], "DELETE is a write ghcli holds back in dry run")


if __name__ == "__main__":
    unittest.main()
