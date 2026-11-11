"""nightly_triage.py: the agent pass between the nightly bug filer and a person.

No real model call anywhere: a FakeModel returns canned answers. The Nov 6-9
fixture is the shape of 2026-11-09: one grader change tripped one safeguard
on cases across two parts of the night, a fix was already open, and the
filer's rule sent the issues to the case owners.
"""

import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import typing
import unittest
import unittest.mock

import test_eval_dashboard_nightly_bugs as nb
import yaml
from eval_dashboard import nightly_bugs, nightly_triage

ISSUES_REPO = "gke-labs/kube-agents-private"
ISSUES = f"repos/{ISSUES_REPO}/issues"
SAFEGUARD = nb.SAFEGUARD
SAFEGUARD_REASON = nb.SAFEGUARD_REASON
CULPRIT, FIX, BYSTANDER, LATER = 4973, 5179, 4990, 5300
GRADER_FILE = "bench/kube_agents_bench/scoring.py"
SHA = {5: "aaaa111", 6: "bbbb222", 7: "cccc333", 8: "dddd444", 9: "eeee555"}
# A live @-mention: what a body filed for triage must not carry.
LIVE_MENTION_RE = re.compile(r"(?<![\w`@/])@[A-Za-z0-9]")
WORKFLOW = nb.WORKFLOW


def night(day, tasks, job=nb.JOB):
    """A nightly build on 2026-11-<day> at main's commit SHA[day], logged to
    the nightly's private bucket."""
    build = f"40000000000000{day:02d}00{1 if job == nb.JOB else 2}"
    return {"build_id": build, "tier": "nightly", "job": job, "pr": None, "head_sha": SHA[day],
            "gcs_base": f"gs://kube-agents-evals-nightly-logs/logs/{job}/{build}/",
            "started": f"2026-11-{day:02d}T00:00:30+00:00", "finished": f"2026-11-{day:02d}T00:59:00+00:00",
            "result": "SUCCESS", "eval_verdict": "GREEN", "duration_s": 3500,
            "tasks": list(tasks) + [nb.green(n) for n in nb.FILLER]}


def november(red_days=(6, 7, 8), green_days=(5,)):
    """Green on ``green_days``, then the safeguard tripped on every case of
    both parts on ``red_days``."""
    runs = []
    for d in sorted(set(green_days) | set(red_days)):
        make = nb.tripped if d in red_days else nb.green
        runs += [night(d, [make(c) for c in nb.MAIN_CASES]), night(d, [make(c) for c in nb.MAIN_B_CASES], job=nb.MAIN_B_JOB)]
    doc = nb.data(*runs)
    doc["generated_at"] = "2026-11-09T12:00:00+00:00"
    return doc


GIT_WINDOW = (
    f"\x1e{SHA[6][:7]}\x1fA Author\x1ffix(bench): attribute card workers' tool calls (#{CULPRIT})\n\n{GRADER_FILE}\nbench/tests/test_scoring.py\n"
    f"\x1ef00f001\x1fB Author\x1fdocs: a typo (#{BYSTANDER})\n\ndocs/README.md\n"
)


class FakeGit:
    """``git log`` over the window and since the first red night."""

    def __init__(self, window=GIT_WINDOW, later=""):
        self.window, self.later, self.calls = window, later, []

    def __call__(self, args):
        self.calls.append(args)
        if any(a.endswith("..HEAD") for a in args):
            return self.later
        return self.window


class FakeSource:
    """GraphQL on the source repository: pull request titles and authors,
    and the open pull requests with their files."""

    def __init__(self, authors=None, open_prs=None, fail=False):
        self.authors = authors or {CULPRIT: "grader-author", BYSTANDER: "docs-author", LATER: "later-author"}
        self.open_prs = open_prs if open_prs is not None else [
            {"number": FIX, "title": "fix(bench): attribute card workers' tool calls to the worker again", "author": {"login": "fixer"},
             "files": {"nodes": [{"path": GRADER_FILE}]}},
            {"number": 5301, "title": "feat: unrelated", "author": {"login": "someone"}, "files": {"nodes": [{"path": "charts/x.yaml"}]}},
        ]
        self.fail = fail
        self.queries = []

    def __call__(self, argv, input=None, **kwargs):
        body = json.loads(input) if input else {}
        self.queries.append(body.get("query", ""))
        if self.fail:
            return nb.GhResult(rc=1, stderr="HTTP 502")
        query = body.get("query", "")
        if "pullRequests(states: OPEN" in query:
            return nb.GhResult(stdout=json.dumps({"data": {"repository": {"pullRequests": {"nodes": self.open_prs}}}}))
        nodes = {}
        for n in map(int, re.findall(r"issueOrPullRequest\(number: (\d+)\)", query)):
            nodes[f"p{n}"] = {"number": n, "title": f"pull request {n}", "author": {"login": self.authors.get(n, "x")}, "mergedAt": "2026-11-05T10:00:00Z"}
        return nb.GhResult(stdout=json.dumps({"data": {"repository": nodes}}))


class FakeIssues:
    """`gh api` on the issues repository: issues, comments, assignment
    (GitHub answers 201 and leaves out a login it will not take), comment
    posts and the closing PATCH."""

    def __init__(self, issues=(), comments=None, refuse=(), fail_assign=False, fail_comment=False, fail_comments_read=False):
        self.issues = {i["number"]: i for i in issues}
        self.comments = comments or {}
        self.refuse = set(refuse)
        self.fail_assign, self.fail_comment, self.fail_comments_read = fail_assign, fail_comment, fail_comments_read
        self.calls = []

    def __call__(self, argv, input=None, **kwargs):
        method, path = argv[argv.index("-X") + 1], argv[argv.index("-X") + 2]
        body = json.loads(input) if input else None
        self.calls.append((method, path, body))
        number = int(m.group(1)) if (m := re.search(r"/issues/(\d+)", path)) else None
        if method == "GET" and path.endswith("comments?per_page=100"):
            if self.fail_comments_read:
                return nb.GhResult(rc=1, stderr="HTTP 502")
            return nb.GhResult(stdout="\n".join(json.dumps(c) for c in self.comments.get(number, [])))
        if method == "GET" and number:
            return nb.GhResult(stdout=json.dumps(self.issues[number])) if number in self.issues else nb.GhResult(rc=1, stderr="HTTP 404")
        if method == "GET":
            return nb.GhResult(stdout="\n".join(json.dumps(i) for i in self.issues.values()))
        if path.endswith("/assignees"):
            if self.fail_assign:
                return nb.GhResult(rc=1, stderr="HTTP 502")
            logins = [x for x in body["assignees"] if x not in self.refuse]
            self.issues[number]["assignees"] = [{"login": x} for x in logins]
            return nb.GhResult(stdout=json.dumps(self.issues[number]))
        if path.endswith("/comments"):
            if self.fail_comment:
                return nb.GhResult(rc=1, stderr="HTTP 502")
            return nb.GhResult(stdout=json.dumps({"id": 1}))
        if method == "PATCH":
            self.issues[number].update(body)
            return nb.GhResult(stdout=json.dumps(self.issues[number]))
        if method == "POST" and path.endswith("/labels"):
            self.issues[number]["labels"] = self.issues[number].get("labels", []) + [{"name": n} for n in body["labels"]]
            return nb.GhResult(stdout=json.dumps(self.issues[number]["labels"]))
        if method == "DELETE" and "/labels/" in path:
            gone = path.rsplit("/", 1)[1].replace("%3A", ":")
            self.issues[number]["labels"] = [lbl for lbl in self.issues[number].get("labels", []) if lbl["name"] != gone]
            return nb.GhResult(stdout=json.dumps(self.issues[number]["labels"]))
        return nb.GhResult(rc=1, stderr="unexpected")

    def writes(self, kind=None):
        out = [(m, p, b) for m, p, b in self.calls if m != "GET"]
        return [w for w in out if kind is None or w[1].endswith(kind) or (kind == "PATCH" and w[0] == "PATCH")]

    def labels(self, number):
        return [lbl["name"] for lbl in self.issues[number].get("labels", [])]

    def posted(self):
        return [b["body"] for m, p, b in self.calls if m == "POST" and p.endswith("/comments")]


def answer(**over):
    base = {"classification": "grader_or_harness", "suspected_pr": CULPRIT, "fix_pr": FIX, "fix_state": "open", "priority": "P1",
            "confidence": "high", "evidence": f"Every case trips {SAFEGUARD}; #{CULPRIT} changed how the grader attributes card workers' calls."}
    base.update(over)
    return json.dumps(base)


class FakeModel:
    """Canned answers in order; an exception instance is raised."""

    def __init__(self, *answers):
        self.answers = list(answers) or [answer()]
        self.prompts, self.max_tokens = [], []

    def __call__(self, prompt, max_output_tokens):
        self.prompts.append(prompt)
        self.max_tokens.append(max_output_tokens)
        out = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(out, Exception):
            raise out
        return out


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.root = self.dir / "repo"
        shutil.copytree(nb.TESTDATA, self.root)
        patch = unittest.mock.patch.object(nightly_bugs.time, "sleep")
        patch.start()
        self.addCleanup(patch.stop)

    def file_pending(self, doc=None):
        """The filer's issue for the fixture, filed for triage, as GitHub
        would hand it back."""
        gh = nb.FakeGh()
        path = self.dir / "data.json"
        path.write_text(json.dumps(doc or november()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            nightly_bugs.main(["--data", str(path), "--repo-root", str(self.root), "--live", "--triage-pending",
                               "--filed-out", str(self.dir / "filed.json")], gh_runner=gh)
        [filed] = [b for m, p, b in gh.writes() if m == "POST" and p.endswith("/issues")]
        return filed, {"number": 37, "state": "open", "title": filed["title"], "body": filed["body"], "assignees": [],
                       "labels": [{"name": n} for n in filed["labels"]], "created_at": "2026-11-09T12:10:00Z"}

    def run_phase(self, argv, issues_gh, source=None, model=None, git=None, gcs=None, now=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nightly_triage.main(argv + ["--repo", ISSUES_REPO, "--repo-root", str(self.root)], gh_runner=issues_gh,
                                     source_runner=source or FakeSource(), model=model, git=git or FakeGit(),
                                     gcs=gcs or (lambda path: f"tail of {path}"), now=now)
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue(), err.getvalue()

    def prepare(self, issues_gh, doc=None, **kw):
        path = self.dir / "data.json"
        path.write_text(json.dumps(doc or november()))
        bundles = self.dir / "bundles.json"
        self.run_phase(["prepare", "--data", str(path), "--out", str(bundles)] + kw.pop("extra", []), issues_gh, **kw)
        return bundles

    def route(self, issues_gh, bundles=None, model=None, live=True, extra=(), **kw):
        argv = ["route"] + (["--bundles", str(bundles)] if bundles else []) + (["--live"] if live else []) + list(extra)
        return self.run_phase(argv, issues_gh, model=model, **kw)


class FilerHandsOffTest(Harness):
    """--triage-pending: the filer files for the agent, not a person."""

    def test_the_issue_is_unassigned_labelled_pending_and_notifies_no_one(self):
        filed, _ = self.file_pending()
        self.assertNotIn("assignees", filed)
        self.assertIn(nightly_bugs.TRIAGE_PENDING_LABEL, filed["labels"])
        self.assertIn("<!-- kube-agents-bot:triage-pending assign=ivan cc=carol,sam,alice,bob -->", filed["body"],
                      "the rule's assignee (the cases span domains: the infra owner) and the case owners it mentions")
        self.assertIsNone(LIVE_MENTION_RE.search(filed["body"]), filed["body"])
        self.assertIn("**Triage pending:**", filed["body"])
        self.assertIn("the issue goes to `ivan`", filed["body"])
        self.assertNotIn("**Assigned to**", filed["body"])
        for case in nb.MAIN_CASES + nb.MAIN_B_CASES:
            self.assertIn(nightly_bugs.MARKER.format(case=case), filed["body"], "the dedupe still reads every case's marker")
        self.assertEqual(json.loads((self.dir / "filed.json").read_text()), [4000])

    def test_a_single_case_issue_names_the_owner_then_the_domain_owner(self):
        doc = nb.data(*(nb.night(d, [nb.red("case-maint")]) for d in (1, 2, 3)))
        _, issue = self.file_pending(doc)
        marker = nightly_triage.pending(issue)
        self.assertEqual(marker.group(1).split(",")[0], nightly_bugs.case_owner.effective_owner(
            nightly_bugs.task_fields("case-maint", self.root)["owner"], "capacity", domain_owners={"capacity": "carol", "security": "sam"}))
        self.assertIsNone(LIVE_MENTION_RE.search(issue["body"]))

    def test_without_the_flag_the_filer_assigns_as_before(self):
        gh = nb.FakeGh()
        path = self.dir / "data.json"
        path.write_text(json.dumps(november()))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            nightly_bugs.main(["--data", str(path), "--repo-root", str(self.root), "--live"], gh_runner=gh)
        [filed] = [b for m, p, b in gh.writes() if m == "POST" and p.endswith("/issues")]
        self.assertEqual(filed["assignees"], ["ivan"])
        self.assertNotIn(nightly_bugs.TRIAGE_PENDING_LABEL, filed["labels"])
        self.assertNotIn("triage-pending", filed["body"])

    def test_quiet_mentions_leaves_emails_and_code_alone(self):
        self.assertEqual(nightly_bugs.quiet_mentions("cc @carol, @sam-b. a@b.c `@x` path/@y"), "cc `carol`, `sam-b`. a@b.c `@x` path/@y")


class NovemberTest(Harness):
    """The 2026-11-09 shape end to end: the group goes to the culprit's author
    with the open fix noted, not to the case owners."""

    def test_the_group_goes_to_the_culprits_author_with_the_open_fix(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue])
        git = FakeGit()
        bundles = self.prepare(issues, git=git)
        self.assertIn(["log", "--no-merges", nightly_triage.LOG_FORMAT, "--name-only", f"-n{nightly_triage.MAX_COMMITS}", f"{SHA[5]}..{SHA[6]}"], git.calls,
                      "the window runs from the last green nightly commit to the first red one")
        model = FakeModel()
        out, _ = self.route(issues, bundles, model)
        [prompt] = model.prompts
        self.assertIn(SAFEGUARD_REASON, prompt, "the failing repetitions' grading lines")
        self.assertIn(f"PR #{CULPRIT} by @grader-author", prompt)
        self.assertIn(f"PR #{FIX} by @fixer", prompt, "the open pull request touching the grader")
        self.assertNotIn("5301", prompt, "an open pull request on other files is not a candidate fix")
        self.assertIn("tail of gs://kube-agents-evals-nightly-logs/logs/ci-kube-agents-eval-nightly/", prompt, "the transcripts the issue links")
        self.assertEqual(model.max_tokens, [nightly_triage.MAX_OUTPUT_TOKENS])

        self.assertEqual([b["assignees"] for m, p, b in issues.writes("/assignees")], [["grader-author"]])
        [comment] = issues.posted()
        self.assertIn("<!-- kube-agents-bot:nightly-triage assignee=grader-author -->", comment)
        self.assertIn(f"https://github.com/gke-labs/kube-agents/pull/{CULPRIT} by @grader-author", comment,
                      "a pull request in the private repository's comment links the source repository in full")
        self.assertIn(f"https://github.com/gke-labs/kube-agents/pull/{FIX} is open", comment)
        self.assertIn("**grader or harness** · suggested priority **P1** · confidence **high**", comment)
        self.assertIn("**Routed to** @grader-author (author of", comment)
        self.assertIn("cc @ivan, @carol, @sam, @alice, @bob", comment, "the rule's assignee and the case owners are mentioned")
        [(_, _, patch)] = issues.writes("PATCH")
        self.assertEqual(set(patch), {"body"}, "labels are added and removed one by one, not replaced")
        self.assertIn(nightly_triage.AGENT_LABEL, issues.labels(37))
        self.assertNotIn(nightly_triage.PENDING_LABEL, issues.labels(37))
        self.assertIn("nightly-red", issues.labels(37))
        self.assertIn("<!-- kube-agents-bot:triage-done assign=ivan cc=carol,sam,alice,bob -->", patch["body"])
        self.assertIn(nightly_triage.TRIAGED_ADDRESSEE, patch["body"])
        self.assertNotIn("Triage pending", patch["body"])
        self.assertIsNone(nightly_triage.pending(patch), "no longer pending")
        self.assertIn(f"#37: grader_or_harness, suspect #{CULPRIT}, fix #{FIX} open, P1, confidence high -> @grader-author", out)
        for case in nb.MAIN_CASES + nb.MAIN_B_CASES:
            self.assertNotIn(case, out, "the job log is public")
        self.assertNotIn(SAFEGUARD, out)

    def test_a_triaged_issue_is_not_triaged_again(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue])
        bundles = self.prepare(issues)
        self.route(issues, bundles, FakeModel())
        model = FakeModel()
        out, _ = self.route(issues, bundles, model)
        self.assertEqual(model.prompts, [])
        self.assertIn("0 issues", out)


class RoutingTableTest(unittest.TestCase):
    """route(): the verdict's choice, then the filer's chain."""

    BUNDLE: typing.ClassVar[dict] = {"prs": {str(CULPRIT): {"author": "grader-author"}, "4999": {"author": "dependabot[bot]"}}}

    def verdict(self, **over):
        v = {"classification": "product", "suspected_pr": CULPRIT, "fix_pr": None, "fix_state": "none", "priority": "P2",
             "confidence": "high", "evidence": "", "dropped_suspect": None}
        v.update(over)
        return v

    def first(self, verdict, failure=None, chain=("carol", "sam")):
        return nightly_triage.route(verdict, failure, self.BUNDLE, list(chain), "ivan")

    def test_product_bug_from_a_named_pr_goes_to_its_author_then_the_rule(self):
        out = self.first(self.verdict())
        self.assertEqual([login for login, _ in out], ["grader-author", "carol", "sam"])
        self.assertIn("author of https://github.com/gke-labs/kube-agents/pull/4973", out[0][1])
        self.assertIn("would not assign `grader-author`", out[1][1])

    def test_grader_from_a_named_pr_goes_to_its_author(self):
        self.assertEqual(self.first(self.verdict(classification="grader_or_harness"))[0][0], "grader-author")

    def test_grader_with_no_pr_goes_to_the_infra_owner(self):
        out = self.first(self.verdict(classification="grader_or_harness", suspected_pr=None))
        self.assertEqual(out[0], ("ivan", nightly_triage.WHY_INFRA_OWNER.format(what="a grading or harness fault")))

    def test_infra_goes_to_the_infra_owner_even_with_a_pr(self):
        self.assertEqual(self.first(self.verdict(classification="infra"))[0][0], "ivan")

    def test_a_case_problem_goes_by_the_rule(self):
        out = self.first(self.verdict(classification="case_rework"))
        self.assertEqual(out[0], ("carol", nightly_triage.WHY_RULE.format(why="triage found a case problem")))

    def test_low_confidence_goes_by_the_rule(self):
        self.assertEqual(self.first(self.verdict(confidence="low"))[0][0], "carol")

    def test_a_product_bug_with_no_pr_goes_by_the_rule(self):
        self.assertEqual(self.first(self.verdict(suspected_pr=None))[0][0], "carol")

    def test_a_bot_authored_pr_goes_by_the_rule(self):
        out = self.first(self.verdict(suspected_pr=4999))
        self.assertEqual(out[0], ("carol", nightly_triage.WHY_RULE.format(why="the suspected pull request's author is a bot")))

    def test_no_verdict_goes_by_the_rule_and_says_why(self):
        out = self.first(None, "the model call failed (TimeoutError)")
        self.assertEqual(out[0], ("carol", "the filing rule's assignee: triage did not decide"))

    def test_the_author_who_is_also_the_rules_assignee_is_listed_once(self):
        self.assertEqual([x for x, _ in self.first(self.verdict(), chain=("grader-author", "sam"))], ["grader-author", "sam"])

    def test_the_model_cannot_name_a_pr_outside_the_window_or_a_fix_outside_the_candidates(self):
        bundle = {"commits": [{"pr": CULPRIT}], "later": [], "open_prs": [{"number": FIX}]}
        v = nightly_triage.parse_verdict(answer(suspected_pr=1234, fix_pr=9999), bundle)
        self.assertIsNone(v["suspected_pr"])
        self.assertEqual(v["dropped_suspect"], 1234)
        self.assertEqual((v["fix_pr"], v["fix_state"]), (None, "none"))
        v = nightly_triage.parse_verdict(answer(fix_state="merged"), bundle)
        self.assertEqual((v["suspected_pr"], v["fix_pr"], v["fix_state"]), (CULPRIT, FIX, "open"), "the state is read from the evidence, not the model")


class FailSafeTest(Harness):
    """Anything that goes wrong routes by the rule, says so once, and still
    marks the issue triaged; a failed GitHub write leaves it for next tick."""

    def triage_with(self, model, **issues_kw):
        _, issue = self.file_pending()
        issues = FakeIssues([issue], **issues_kw)
        bundles = self.prepare(issues)
        out, _ = self.route(issues, bundles, model)
        return issues, out

    def assert_rule(self, issues, why):
        self.assertEqual([b["assignees"] for m, p, b in issues.writes("/assignees")], [["ivan"]])
        [comment] = issues.posted()
        self.assertIn("**Agent triage did not run:**", comment)
        self.assertIn(why, comment)
        self.assertIn("Routed by the filing rule to @ivan", comment)
        self.assertEqual(len([line for line in comment.splitlines() if line.strip() and not line.startswith("<!--")]), 1, "one line")
        self.assertEqual(len(issues.writes("PATCH")), 1)
        self.assertIn(nightly_triage.AGENT_LABEL, issues.labels(37))

    def test_a_failed_model_call(self):
        issues, out = self.triage_with(FakeModel(nightly_triage.ModelError("the model call failed (HTTP 403)")))
        self.assert_rule(issues, "the model call failed (HTTP 403)")
        self.assertIn("#37: rule (triage did not decide) -> @ivan", out)

    def test_a_timeout(self):
        self.assert_rule(self.triage_with(FakeModel(nightly_triage.ModelError("the model call failed (TimeoutError)")))[0], "TimeoutError")

    def test_an_unparseable_answer(self):
        self.assert_rule(self.triage_with(FakeModel("I think it is the grader"))[0], "the model's answer was not JSON")

    def test_an_answer_off_the_schema(self):
        self.assert_rule(self.triage_with(FakeModel(answer(classification="maybe")))[0], "no valid classification")

    def test_a_crash_inside_the_model_client(self):
        self.assert_rule(self.triage_with(FakeModel(KeyError("candidates")))[0], "triage raised KeyError")

    def test_the_vertex_client_turns_errors_into_model_errors(self):
        ok = subprocess.CompletedProcess([], 0, stdout="tok\n", stderr="")

        def opener(status=None, payload=None):
            def call(request, timeout):
                self.assertEqual(json.loads(request.data)["generationConfig"]["maxOutputTokens"], 10)
                self.assertIn("/models/gemini-4-argon:generateContent", request.full_url)
                if status:
                    raise nightly_triage.urllib.error.HTTPError(request.full_url, status, "no", {}, None)
                return contextlib.nullcontext(io.BytesIO(json.dumps(payload).encode()))
            return call
        client = nightly_triage.VertexModel("p", "gemini-4-argon", runner=lambda *a, **k: ok, opener=opener(403))
        with self.assertRaisesRegex(nightly_triage.ModelError, "HTTP 403"):
            client("x", 10)
        client.opener = opener(payload={"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{"}]}}]})
        with self.assertRaisesRegex(nightly_triage.ModelError, "MAX_TOKENS"):
            client("x", 10)
        client.opener = opener(payload={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"thought": True, "text": "hmm"}, {"text": "{}"}]}}]})
        self.assertEqual(client("x", 10), "{}", "thinking parts are not the answer")
        client.runner = lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="no creds")
        with self.assertRaisesRegex(nightly_triage.ModelError, "no access token"):
            client("x", 10)

    def test_no_evidence_yet_waits_for_the_next_tick(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue])
        model = FakeModel()
        now = nightly_triage.nightly_bugs.nightly.parse_iso("2026-11-09T12:40:00Z")
        out, _ = self.route(issues, self.dir / "missing.json", model, now=now)
        self.assertEqual(issues.writes(), [])
        self.assertEqual(model.prompts, [])
        self.assertIn("#37: skipped", out)

    def test_no_evidence_for_too_long_goes_by_the_rule(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue])
        now = nightly_triage.nightly_bugs.nightly.parse_iso("2026-11-09T15:00:00Z")
        self.route(issues, None, FakeModel(), now=now)
        self.assert_rule(issues, "no evidence was gathered in the 2 hours since filing")

    def test_no_model_routes_by_the_rule_without_a_call(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue])
        model = FakeModel()
        self.route(issues, None, model, extra=["--no-model"])
        self.assertEqual(model.prompts, [])
        self.assert_rule(issues, "triage is switched off")

    def test_an_author_github_will_not_assign_falls_back_to_the_rule(self):
        issues, _ = self.triage_with(FakeModel(), refuse=("grader-author",))
        self.assertEqual([b["assignees"] for m, p, b in issues.writes("/assignees")], [["grader-author"], ["ivan"]])
        [comment] = issues.posted()
        self.assertIn("**Routed to** @ivan (the filing rule's next choice: GitHub would not assign `grader-author`)", comment)

    def test_nobody_assignable_still_comments_and_finishes(self):
        issues, _ = self.triage_with(FakeModel(), refuse=("grader-author", "ivan"))
        [comment] = issues.posted()
        self.assertIn("**Routed to** no one (GitHub would not assign `grader-author`, `ivan`)", comment)
        self.assertEqual(len(issues.writes("PATCH")), 1)

    def test_a_failed_assignment_call_writes_nothing_more(self):
        issues, out = self.triage_with(FakeModel(), fail_assign=True)
        self.assertEqual(issues.posted(), [])
        self.assertEqual(issues.writes("PATCH"), [])
        self.assertIn("NOT DONE", out)

    def test_a_failed_comment_leaves_it_pending_and_the_next_tick_keeps_the_assignee(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue], fail_comment=True)
        bundles = self.prepare(issues)
        self.route(issues, bundles, FakeModel())
        self.assertEqual(issues.writes("PATCH"), [])
        issues.fail_comment = False
        issues.calls.clear()
        model = FakeModel(answer(suspected_pr=None, classification="case_rework"))
        self.route(issues, bundles, model)
        self.assertEqual(model.prompts, [], "no second opinion to pair with the first run's assignee")
        self.assertEqual(issues.writes("/assignees"), [], "an assignee already set is kept")
        [comment] = issues.posted()
        self.assertIn("<!-- kube-agents-bot:nightly-triage assignee=grader-author -->", comment)
        self.assertIn("already had an assignee, @grader-author", comment)
        self.assertNotIn("filing rule", comment)
        self.assertEqual(len(issues.writes("PATCH")), 1, "and it is marked done")

    def test_a_comment_from_an_earlier_run_is_not_posted_again(self):
        _, issue = self.file_pending()
        earlier = {37: [{"body": "<!-- kube-agents-bot:nightly-triage assignee=grader-author -->\nAgent triage ..."}]}
        issues = FakeIssues([issue], comments=earlier)
        bundles = self.prepare(issues)
        model = FakeModel()
        self.route(issues, bundles, model)
        self.assertEqual(model.prompts, [], "an issue that only needs finishing costs no model call")
        self.assertEqual(issues.posted(), [])
        self.assertEqual(issues.writes("/assignees"), [])
        self.assertEqual(len(issues.writes("PATCH")), 1)

    def test_unreadable_comments_write_nothing(self):
        issues, out = self.triage_with(FakeModel(), fail_comments_read=True)
        self.assertEqual(issues.writes(), [])
        self.assertIn("NOT DONE", out)

    def test_evidence_that_could_not_be_read_is_named_to_the_model(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue])
        bundles = self.prepare(issues, source=FakeSource(fail=True), gcs=lambda path: None)
        model = FakeModel()
        self.route(issues, bundles, model)
        self.assertIn("the pull requests' titles and authors could not be read", model.prompts[0])
        self.assertIn("transcript not readable", model.prompts[0])
        self.assertEqual([b["assignees"] for m, p, b in issues.writes("/assignees")], [["ivan"]],
                         "no author to assign: the suspected pull request's author is unknown, so the rule")


class ReviewFindingsTest(Harness):
    """The self-review's findings, pinned."""

    def test_live_triage_refuses_the_public_repository(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nightly_triage.main(["route", "--live", "--repo", "gke-labs/kube-agents", "--repo-root", str(self.root)], gh_runner=issues, model=FakeModel())
        self.assertEqual(rc, 0)
        self.assertEqual(issues.calls, [], "not even a read")
        self.assertIn("public", err.getvalue())

    def test_the_models_evidence_and_a_pr_title_mention_no_one(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue])
        source = FakeSource()
        bundles = self.prepare(issues, source=source)
        doc = json.loads(bundles.read_text())
        doc["37"]["prs"][str(CULPRIT)]["title"] = "ping @everyone"
        bundles.write_text(json.dumps(doc))
        self.route(issues, bundles, FakeModel(answer(evidence="Caused by @grader-author and @someone-else.")))
        [comment] = issues.posted()
        self.assertIn("Caused by @\u200bgrader-author and @\u200bsomeone-else.", comment)
        self.assertIn("ping @\u200beveryone", comment)

    def test_defang_survives_backticks(self):
        for text in ("fix `@everyone", "`foo`@alice", "`cmd @bob` `@alice`"):
            self.assertIsNone(re.search(r"@[A-Za-z0-9]", nightly_triage.defang(text)), text)

    def test_a_single_case_issue_keeps_its_domain_owner_when_marked_done(self):
        doc = nb.data(*(nb.night(d, [nb.red("case-maint")]) for d in (1, 2, 3)))
        _, issue = self.file_pending(doc)
        self.assertIn("Domain owner: `carol`", issue["body"])
        issues = FakeIssues([issue])
        self.route(issues, None, None, extra=["--no-model"])
        [(_, _, patch)] = issues.writes("PATCH")
        self.assertIn(nightly_triage.TRIAGED_ADDRESSEE, patch["body"])
        self.assertIn("Domain owner: `carol`", patch["body"])

    def test_a_failed_open_pr_read_is_noted_on_every_issue(self):
        _, issue = self.file_pending()
        issues = FakeIssues([issue, dict(issue, number=38, created_at="2026-11-09T12:20:00Z")])

        class OpenPrsDown(FakeSource):
            def __call__(self, argv, input=None, **kwargs):
                if "pullRequests(states: OPEN" in (json.loads(input) if input else {}).get("query", ""):
                    self.queries.append("open")
                    return nb.GhResult(rc=1, stderr="HTTP 502")
                return super().__call__(argv, input, **kwargs)
        source = OpenPrsDown()
        bundles = json.loads(self.prepare(issues, source=source).read_text())
        for number in ("37", "38"):
            self.assertIn("the open pull requests could not be read", bundles[number]["notes"])
        self.assertEqual(source.queries.count("open"), 2, "a failed read is tried again")

    def test_the_backstop_takes_only_issues_waiting_past_the_limit(self):
        _, issue = self.file_pending()
        young = dict(issue, number=38, created_at="2026-11-09T14:30:00Z")
        issues = FakeIssues([issue, young])
        now = nightly_triage.nightly_bugs.nightly.parse_iso("2026-11-09T15:00:00Z")
        out, _ = self.route(issues, None, None, extra=["--no-model", "--stale-only"], now=now)
        self.assertIn("1 issues", out)
        self.assertEqual({p.split("/issues/")[1].split("/")[0] for m, p, b in issues.writes()}, {"37"})
        [comment] = issues.posted()
        self.assertIn("the triage job did not route this issue in the 2 hours since filing", comment)
        self.assertNotIn("switched off", comment)

    def test_the_evidence_is_the_streak_that_filed_the_issue(self):
        def v(day, outcome):
            return {"date": f"2026-11-{day:02d}", "outcome": outcome}
        passed, failed, partial = nightly_triage.classify.OUTCOME_PASSED, nightly_triage.classify.OUTCOME_FAILED, nightly_triage.classify.OUTCOME_PARTIAL
        # Red 6-8 (the filed streak), clear 9, red once more on 10.
        nights = [v(5, passed), v(6, failed), v(7, failed), v(8, failed), v(9, passed), v(10, failed)]
        red, green = nightly_triage.last_red_block(nights)
        self.assertEqual([n["date"][-2:] for n in red], ["06", "07", "08"])
        self.assertEqual(green["date"], "2026-11-05")
        nights[4] = v(9, partial)
        red, green = nightly_triage.last_red_block(nights)
        self.assertEqual([n["date"][-2:] for n in red], ["06", "07", "08"])
        self.assertEqual(green["date"], "2026-11-05")
        # No streak long enough on record: the newest failing run.
        red, green = nightly_triage.last_red_block([v(5, passed), v(6, failed), v(7, passed), v(8, failed)])
        self.assertEqual(([n["date"][-2:] for n in red], green["date"]), (["08"], "2026-11-07"))

    def test_a_grader_fault_whose_author_cannot_be_read_goes_to_the_infra_owner(self):
        out = nightly_triage.route({"classification": "grader_or_harness", "suspected_pr": CULPRIT, "confidence": "high"}, None, {"prs": {}}, ["carol"], "ivan")
        self.assertEqual(out[0][0], "ivan")
        out = nightly_triage.route({"classification": "product", "suspected_pr": CULPRIT, "confidence": "high"}, None, {"prs": {}}, ["carol"], "ivan")
        self.assertEqual(out[0], ("carol", nightly_triage.WHY_RULE.format(why="the suspected pull request's author could not be read")))

    def test_a_pr_number_that_is_an_issue_does_not_lose_the_other_authors(self):
        self.assertIn("issueOrPullRequest(number: %(n)d) { ... on PullRequest", nightly_triage.PR_FIELD)

    def test_an_edit_made_while_triage_ran_is_kept(self):
        _, issue = self.file_pending()
        issues = FakeIssues([dict(issue)])
        bundles = self.prepare(issues)
        split = nightly_bugs.MARKER.format(case="case-maint")

        def meanwhile(prompt, max_output_tokens):
            # A triager splits a case off and adds a label while the model
            # thinks, after the step listed the issue.
            issues.issues[37] = dict(issues.issues[37], body=issues.issues[37]["body"].replace(split + "\n", ""),
                                     labels=[*issues.issues[37]["labels"], {"name": "P1"}])
            return answer()
        self.route(issues, bundles, meanwhile)
        self.assertNotIn(split, issues.issues[37]["body"])
        self.assertIn("P1", issues.labels(37))


class CapsTest(Harness):
    def test_at_most_max_issues_per_run(self):
        _, issue = self.file_pending()
        many = [dict(issue, number=n, created_at=f"2026-11-09T12:{n:02d}:00Z") for n in range(30, 37)]
        issues = FakeIssues(many)
        bundles = self.prepare(issues)
        self.assertEqual(len(json.loads(bundles.read_text())), nightly_triage.MAX_ISSUES_PER_RUN)
        model = FakeModel()
        _, err = self.route(issues, bundles, model)
        self.assertEqual(len(model.prompts), nightly_triage.MAX_ISSUES_PER_RUN)
        self.assertEqual(sorted({int(p.split("/issues/")[1].split("/")[0]) for m, p, b in issues.writes()}), list(range(30, 35)), "the oldest first")
        self.assertIn("the rest next tick", err)

    def test_a_long_transcript_is_cut_to_the_token_cap(self):
        bundle = {"cases": [], "commits": [], "transcripts": [{"case": "c", "night": "d", "rep": 1, "tail": "x" * 500_000}]}
        prompt = nightly_triage.build_prompt(bundle)
        self.assertLessEqual(len(prompt), nightly_triage.MAX_INPUT_TOKENS * nightly_triage.CHARS_PER_TOKEN)
        self.assertIn("x" * 1000, prompt, "cut, not dropped, while a cut fits")

    def test_commits_are_cut_before_grading_lines(self):
        commits = [{"sha": f"{i:07x}", "pr": None, "subject": "s" * 200, "files": []} for i in range(400)]
        bundle = {"cases": [{"case": "c", "last_green": None, "red_nights": [{"date": "d", "head_sha": "h", "fails": [{"reason": "the-grading-line"}]}]}],
                  "commits": commits}
        prompt = nightly_triage.build_prompt(bundle, max_chars=20_000)
        self.assertIn("the-grading-line", prompt)
        self.assertLess(prompt.count(" no PR: "), 400)

    def test_evidence_over_the_cap_is_never_sent(self):
        night = {"date": "d", "head_sha": "h", "fails": [{"reason": "r" * 400_000}]}
        bundle = {"cases": [{"case": "c", "last_green": None, "red_nights": [night]}], "commits": []}
        model = FakeModel()
        verdict, failure = nightly_triage.decide(bundle, model)
        self.assertIsNone(verdict)
        self.assertIn("token cap", failure)
        self.assertEqual(model.prompts, [])


class DryRunTest(Harness):
    def test_a_dry_run_writes_nothing_and_prints_no_case(self):
        # An issue the filer assigned the old way (triage unarmed): the
        # current assignee is the rule's answer.
        gh = nb.FakeGh()
        path = self.dir / "data.json"
        path.write_text(json.dumps(november()))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            nightly_bugs.main(["--data", str(path), "--repo-root", str(self.root), "--live"], gh_runner=gh)
        [filed] = [b for m, p, b in gh.writes() if m == "POST" and p.endswith("/issues")]
        issue = {"number": 37, "state": "open", "title": filed["title"], "body": filed["body"], "assignees": [{"login": "ivan"}], "labels": [],
                 "created_at": "2026-11-09T12:10:00Z"}
        issues = FakeIssues([issue, {"number": 38, "state": "open", "title": "infra", "body": "<!-- kube-agents-bot:nightly-lost job=x -->"}])
        bundles = self.prepare(issues, extra=["--issue", "37", "--issue", "38"])
        out, err = self.route(issues, bundles, FakeModel(), live=False, extra=["--issue", "37", "--issue", "38"])
        self.assertEqual(issues.writes(), [])
        self.assertIn("nightly triage (dry run, gemini-4-argon): 1 issues", out, "an infra issue is not triaged")
        self.assertIn(f"#37: grader_or_harness, suspect #{CULPRIT}, fix #{FIX} open, P1, confidence high -> @grader-author", out)
        for text in (out, err):
            for case in nb.MAIN_CASES + nb.MAIN_B_CASES:
                self.assertNotIn(case, text)
            self.assertNotIn(SAFEGUARD, text)

    def test_the_model_defaults_to_argon_and_a_variable_overrides_it(self):
        self.assertEqual(nightly_triage.DEFAULT_MODEL, "gemini-4-argon")
        with unittest.mock.patch.dict(os.environ, {nightly_triage.MODEL_ENV: "gemini-other"}):
            self.assertEqual(nightly_triage.parse_args(["route"]).model, "gemini-other")
        with unittest.mock.patch.dict(os.environ, {nightly_triage.MODEL_ENV: ""}):
            self.assertEqual(nightly_triage.parse_args(["route"]).model, "gemini-4-argon")


class WorkflowTest(unittest.TestCase):
    """ci-health.yml: armed separately, off by default, dry otherwise."""

    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.doc = yaml.safe_load(self.text)

    def run_step(self, step, env):
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "python3"
            fake.write_text('#!/bin/sh\necho "$@"\n')
            fake.chmod(0o755)
            out = pathlib.Path(tmp) / "out"
            full = {"PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}", "GITHUB_OUTPUT": str(out), **env}
            proc = subprocess.run(["bash", "-e", "-c", step["run"]], env=full, capture_output=True, text=True, check=False)
            outputs = out.read_text() if out.exists() else ""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout, outputs

    def test_the_switches_default_off_and_argon(self):
        self.assertIn("NIGHTLY_TRIAGE_LIVE: ${{ vars.NIGHTLY_TRIAGE_LIVE || 'false' }}", self.text)
        self.assertIn("NIGHTLY_TRIAGE_MODEL: ${{ vars.NIGHTLY_TRIAGE_MODEL || 'gemini-4-argon' }}", self.text)

    def test_the_filer_files_for_triage_only_when_both_are_armed(self):
        [step] = [s for s in self.doc["jobs"]["refresh-and-adjudicate"]["steps"] if s.get("id") == "nightly_bugs"]
        for bugs, triage, mute, want in (("true", "true", "false", True), ("true", "false", "false", False),
                                         ("false", "true", "false", False), ("true", "true", "true", False)):
            out, _ = self.run_step(step, {"NIGHTLY_BUGS_LIVE": bugs, "NIGHTLY_TRIAGE_LIVE": triage, "CI_HEALTH_MUTE": mute,
                                           "KUBE_AGENTS_ISSUES_REPO": ISSUES_REPO})
            self.assertEqual("--triage-pending" in out.split(), want, (bugs, triage, mute))
        out, _ = self.run_step(step, {"NIGHTLY_BUGS_LIVE": "true", "NIGHTLY_TRIAGE_LIVE": "true", "CI_HEALTH_MUTE": "false", "KUBE_AGENTS_ISSUES_REPO": ""})
        self.assertNotIn("--triage-pending", out.split(), "never for issues filed in the public repository")

    def test_the_triage_job_runs_after_the_tick_on_main_with_its_own_identities(self):
        job = self.doc["jobs"]["nightly-triage"]
        self.assertEqual(job["needs"], "refresh-and-adjudicate")
        for guard in ("github.repository == 'gke-labs/kube-agents'", "github.ref == 'refs/heads/main'", "vars.CI_HEALTH_MUTE != 'true'"):
            self.assertIn(guard, job["if"])
        accounts = [s["with"]["service_account"] for s in job["steps"] if "google-github-actions/auth" in str(s.get("uses"))]
        self.assertEqual(accounts, ["${{ env.COLLECT_SA }}", "${{ env.CI_HEALTH_SA }}"], "evidence as the reader, the model as the publisher")
        self.assertNotIn("pull-requests", job["permissions"])
        names = [s.get("name") for s in job["steps"]]
        self.assertLess(names.index("Gather the evidence for each issue"), names.index("Authenticate to Google Cloud (publisher, for the model call)"))
        self.assertEqual(self.doc["jobs"]["refresh-and-adjudicate"]["outputs"]["filed"], "${{ steps.nightly_bugs.outputs.filed }}")

    def test_the_plan_is_live_only_when_armed_and_a_dispatch_is_always_dry(self):
        [step] = [s for s in self.doc["jobs"]["nightly-triage"]["steps"] if s.get("id") == "plan"]
        base = {"NIGHTLY_BUGS_LIVE": "true", "NIGHTLY_TRIAGE_LIVE": "true", "FILED": "41", "TRIAGE_ISSUES": "", "ISSUES_REPO": ISSUES_REPO}
        _, outputs = self.run_step(step, dict(base, ISSUES_REPO=""))
        self.assertIn("mode=--dry-run", outputs, "no private issues repository: never live")
        _, outputs = self.run_step(step, base)
        self.assertIn("mode=--live", outputs)
        self.assertIn("args=\n", outputs, "live: every pending issue")
        _, outputs = self.run_step(step, dict(base, NIGHTLY_TRIAGE_LIVE="false"))
        self.assertIn("mode=--dry-run", outputs)
        self.assertIn("args= --issue 41", outputs)
        _, outputs = self.run_step(step, dict(base, TRIAGE_ISSUES="37 33"))
        self.assertIn("mode=--dry-run", outputs)
        self.assertIn("args= --issue 37 --issue 33", outputs)

    def test_a_dispatch_input_that_is_not_digits_is_refused(self):
        [step] = [s for s in self.doc["jobs"]["nightly-triage"]["steps"] if s.get("id") == "plan"]
        for bad in ("1; rm -rf /", "   "):
            proc = subprocess.run(["bash", "-e", "-c", step["run"]], env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": "/dev/null", "TRIAGE_ISSUES": bad},
                                  capture_output=True, text=True, check=False)
            self.assertNotEqual(proc.returncode, 0, repr(bad))

    def test_the_backstop_routes_every_waiting_issue_when_off_and_stale_ones_when_on(self):
        [step] = [s for s in self.doc["jobs"]["refresh-and-adjudicate"]["steps"] if s.get("name", "").startswith("Route issues left waiting")]
        self.assertNotIn("NIGHTLY_TRIAGE_LIVE", step["if"], "it runs whether triage is on or off")
        self.assertTrue(step["continue-on-error"])
        out, _ = self.run_step(step, {"NIGHTLY_TRIAGE_LIVE": "false"})
        self.assertEqual(out.split(), ["scripts/eval_dashboard/nightly_triage.py", "route", "--no-model", "--live"])
        out, _ = self.run_step(step, {"NIGHTLY_TRIAGE_LIVE": "true"})
        self.assertEqual(out.split()[-1], "--stale-only")

    def test_the_triage_job_cannot_write_issues_with_its_own_token(self):
        self.assertNotIn("issues", self.doc["jobs"]["nightly-triage"]["permissions"])


if __name__ == "__main__":
    unittest.main()
