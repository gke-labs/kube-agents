"""Tests for .github/workflows/needs-triage.yml.

The workflow keeps the `needs-triage` issue label equal to "open, non-PR issue
with no `priority:*` label". Its decision lives in a jq program and a shell
loop inside the step's `run:` block, and its reach lives in the job's `if:` and
trigger list; nothing else exercises either, so an edit that drops the
pull-request filter, breaks the quoting around the label prefix, drops
`--paginate` from the sweep, narrows the trigger set, or lets a label that
vanished between read and write abort the rest of a sweep would ship silently.

The script tests run the `run:` block verbatim -- extracted from the YAML,
never re-implemented, under the environment names the step's `env:` binds --
against a fake `gh` on PATH that serves fixture issues through the real `jq`,
one `per_page` page at a time the way `gh` does, fails the way `gh` fails
(`gh: <message> (HTTP <status>)` on stderr, exit 1), and records every write,
so what is tested is the shell GitHub will run.
"""

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "needs-triage.yml"

_REPO = "gke-labs/kube-agents"
_LABEL = "needs-triage"
_PRIORITY_PREFIX = "priority:"

# What the step binds from the event, and nothing else: the script reads only
# these names, `GITHUB_EVENT_NAME` (a runner default), and `PATH`.
_STEP_ENV = {
    "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
    "REPO": "${{ github.repository }}",
    "ISSUE_NUMBER": "${{ github.event.issue.number }}",
    "DRY_RUN": "${{ inputs.dry_run }}",
}
_CONCURRENCY_GROUP = "${{ github.workflow }}-${{ github.event.issue.number || 'sweep' }}"

# The sweep's page size, read from the script so the filler below fills one
# page exactly and pushes the whole decision table onto the second: a sweep
# that stops paginating then misses every write the tests expect.
_PAGE_SIZE_RE = re.compile(r"^\s*PAGE_SIZE=(\d+)", re.MULTILINE)

# The fake `gh`: `api` reads are answered from FAKE_GH_ISSUES through jq with
# the program the caller passed; `-X POST`/`-X DELETE` are appended to
# FAKE_GH_WRITES. The list endpoint is served the way GitHub and `gh` serve it:
# `per_page` items per page, only the first page unless `--paginate` is given,
# and the jq program applied to each page separately. Failures are shaped like
# gh's: FAKE_GH_FAIL_WRITES fails every write with a 500; FAKE_GH_DELETE_404
# (comma-separated issue numbers) answers a DELETE on those issues the way
# GitHub answers a label that is no longer there; FAKE_GH_MISSING (likewise)
# answers a single-issue GET, or a POST, the way GitHub answers a deleted issue.
_FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, re, subprocess, sys
    argv = sys.argv[1:]
    assert argv and argv[0] == "api", argv
    method, path, program, paginate = "GET", None, None, False
    it = iter(argv[1:])
    for arg in it:
        if arg == "-X":
            method = next(it)
        elif arg == "--jq":
            program = next(it)
        elif arg == "--paginate":
            paginate = True
        elif arg in ("-f", "-F"):
            next(it)
        elif arg.startswith("-"):
            continue
        else:
            path = arg
    def fail(message, status):
        print(json.dumps({"message": message, "status": str(status)}))
        print(f"gh: {message} (HTTP {status})", file=sys.stderr)
        sys.exit(1)
    def listed(name):
        return {int(n) for n in os.environ.get(name, "").split(",") if n}
    issue_in_path = int(re.search(r"/issues/(\\d+)", path).group(1)) if re.search(r"/issues/(\\d+)", path) else None
    if method != "GET":
        with open(os.environ["FAKE_GH_WRITES"], "a") as log:
            log.write(" ".join([method] + argv[1:]) + "\\n")
        if os.environ.get("FAKE_GH_FAIL_WRITES"):
            fail("Internal Server Error", 500)
        if method == "DELETE" and issue_in_path in listed("FAKE_GH_DELETE_404"):
            fail("Label does not exist", 404)
        if method == "POST" and issue_in_path in listed("FAKE_GH_MISSING"):
            fail("Not Found", 404)
        sys.exit(0)
    issues = json.load(open(os.environ["FAKE_GH_ISSUES"]))
    single = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\\d+)", path)
    if single:
        if issue_in_path in listed("FAKE_GH_MISSING"):
            fail("Not Found", 404)
        (data,) = [i for i in issues if i["number"] == issue_in_path]
        pages = [data]
    else:
        assert path.startswith("repos/") and "/issues?" in path, path
        per_page = int(re.search(r"per_page=(\\d+)", path).group(1))
        pages = [issues[i : i + per_page] for i in range(0, len(issues), per_page)] or [[]]
        if not paginate:
            pages = pages[:1]
    for page in pages:
        if subprocess.run(["jq", "-r", program], input=json.dumps(page), text=True).returncode:
            sys.exit(1)
    """
)


def _issue(number, labels=(), state="open", pull_request=False):
    issue = {"number": number, "state": state, "labels": [{"name": name} for name in labels]}
    if pull_request:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/{_REPO}/pulls/{number}"}
    return issue


# One issue per cell of the decision table, plus the shapes that must be left alone.
_TABLE = [
    _issue(1),  # open, nothing: add
    _issue(2, [_LABEL]),  # already queued: keep
    _issue(3, [_LABEL, "priority:p2"]),  # prioritised and still queued: remove
    _issue(4, ["priority:p1"]),  # prioritised: keep
    _issue(5, state="closed"),  # closed: untouched
    _issue(6, pull_request=True),  # a pull request: untouched
    _issue(7, ["kind/priority:high"]),  # not the prefix: add
    _issue(8, [_LABEL, "priority:p0", "kind/bug"]),  # remove, whatever else it carries
]
_EXPECTED_ADDS = {1, 7}
_EXPECTED_REMOVES = {3, 8}


def _fixture(page_size):
    """A full page of already-consistent issues, then the decision table."""
    filler = [_issue(1000 + i, ["priority:p3"]) for i in range(page_size)]
    return filler + _TABLE


def _load():
    return yaml.safe_load(_WORKFLOW.read_text())


def _job():
    (job,) = _load()["jobs"].values()
    return job


def _step():
    (step,) = _job()["steps"]
    return step


class TriggerAndGuardTest(unittest.TestCase):
    def setUp(self):
        self.workflow = _load()
        self.job = _job()
        self.triggers = self.workflow[True]  # PyYAML reads the `on:` key as boolean True

    def test_runs_on_the_issue_events_that_can_break_the_rule(self):
        """A label swap fires `unlabeled` then `labeled`; both must reach the job, as must a reopen."""
        self.assertEqual(set(self.triggers["issues"]["types"]), {"opened", "reopened", "labeled", "unlabeled"})
        self.assertIn("dry_run", self.triggers["workflow_dispatch"]["inputs"])

    def test_a_scheduled_sweep_backs_the_events(self):
        """An issue a bot opens with GITHUB_TOKEN fires no `issues` event; only a sweep queues it."""
        (entry,) = self.triggers["schedule"]
        self.assertRegex(entry["cron"], r"^\S+ \S+ \S+ \S+ \S+$")
        self.assertIn("github.event_name == 'schedule'", self.job["if"])

    def test_job_is_guarded_and_filtered(self):
        """The fork guard and the pull-request guard are conjuncts over the whole event filter, not alternatives inside it."""
        condition = " ".join(self.job["if"].split())
        self.assertEqual(
            condition,
            "github.repository == 'gke-labs/kube-agents' && "
            "!github.event.issue.pull_request && "
            "(github.event_name == 'workflow_dispatch' || "
            "github.event_name == 'schedule' || "
            "github.event.action == 'opened' || "
            "github.event.action == 'reopened' || "
            f"github.event.label.name == '{_LABEL}' || "
            f"startsWith(github.event.label.name, '{_PRIORITY_PREFIX}'))",
        )

    def test_token_reaches_issues_and_nothing_else(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["permissions"], {"issues": "write"})

    def test_no_third_party_action_and_no_checkout(self):
        """Nothing to pin, and no working tree for the write token to run code from."""
        self.assertEqual([step for step in self.job["steps"] if "uses" in step], [])

    def test_step_binds_exactly_the_event_fields_the_script_reads(self):
        """Event fields reach the shell through `env:`, never by interpolation, and only these."""
        self.assertEqual(_step()["env"], _STEP_ENV)
        self.assertNotIn("${{", _step()["run"])

    def test_concurrency_serialises_per_issue_and_sweeps_with_each_other(self):
        """Job-level, so a run the `if:` skips is not expected to displace a pending run; one group per issue for events and one shared group for sweeps; never cancelled, so the pending run reads what the running one wrote."""
        self.assertNotIn("concurrency", self.workflow)
        concurrency = self.job["concurrency"]
        self.assertEqual(concurrency["group"], _CONCURRENCY_GROUP)
        self.assertIs(concurrency["cancel-in-progress"], False)


class ScriptTest(unittest.TestCase):
    def setUp(self):
        if not shutil.which("jq"):
            # The fake `gh` answers reads through jq. A developer machine without it skips;
            # a CI runner without it has lost the half of this file that tests the shell,
            # and must say so rather than report green on the YAML shape alone.
            if os.environ.get("GITHUB_ACTIONS"):
                self.fail("jq is not on PATH; the script tests cannot run")
            self.skipTest("jq is not on PATH")
        step = _step()
        self.script = step["run"]
        self.env_names = set(step["env"])
        self.page_size = int(_PAGE_SIZE_RE.search(self.script).group(1))
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="needs-triage-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        gh = self.tmp / "gh"
        gh.write_text(_FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.issues = self.tmp / "issues.json"
        self.issues.write_text(json.dumps(_fixture(self.page_size)))
        self.writes = self.tmp / "writes.log"
        self.writes.touch()

    def _env(self, event, issue_number="", dry_run="", **fake):
        # The step's own bindings, by the names the YAML declares, so a renamed
        # or dropped binding fails here rather than only on a runner.
        bound = {"GH_TOKEN": "fake", "REPO": _REPO, "ISSUE_NUMBER": issue_number, "DRY_RUN": dry_run}
        self.assertEqual(set(bound), self.env_names)
        env = {
            # The fake `gh` first; the rest inherited so `jq` and `python3` are found wherever this machine keeps them.
            "PATH": f"{self.tmp}{os.pathsep}{os.environ.get('PATH', '')}",
            "GITHUB_EVENT_NAME": event,
            "FAKE_GH_ISSUES": str(self.issues),
            "FAKE_GH_WRITES": str(self.writes),
            **bound,
        }
        env.update({f"FAKE_GH_{name.upper()}": value for name, value in fake.items()})
        return env

    def _run(self, event, issue_number="", dry_run="", script=None, **fake):
        # GitHub runs `shell: bash` as `bash --noprofile --norc -eo pipefail`.
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script or self.script],
            env=self._env(event, issue_number, dry_run, **fake),
            capture_output=True,
            text=True,
        )

    def _writes(self):
        """Every write attempted, in order: (method, issue number)."""
        attempts = []
        for line in self.writes.read_text().splitlines():
            method, _, rest = line.partition(" ")
            number = int(rest.split(f"repos/{_REPO}/issues/", 1)[1].split("/", 1)[0])
            if method == "POST":
                self.assertIn(f"labels[]={_LABEL}", rest)
            else:
                self.assertEqual(method, "DELETE")
                self.assertTrue(rest.split()[-1].endswith(f"/labels/{_LABEL}"), rest)
            attempts.append((method, number))
        return attempts

    def _adds_and_removes(self):
        attempts = self._writes()
        return {n for m, n in attempts if m == "POST"}, {n for m, n in attempts if m == "DELETE"}

    def test_sweep_reconciles_every_open_issue_across_pages(self):
        """Dispatch and schedule are one sweep, and the decision table sits past the first page."""
        for event in ("workflow_dispatch", "schedule"):
            with self.subTest(event=event):
                self.writes.write_text("")
                result = self._run(event)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self._adds_and_removes(), (_EXPECTED_ADDS, _EXPECTED_REMOVES))
                self.assertIn(f"{len(_EXPECTED_ADDS) + len(_EXPECTED_REMOVES)} change(s), 0 skipped", result.stdout)

    def test_sweep_without_paginate_would_miss_the_second_page(self):
        """Proves the test above depends on `--paginate`: the first page alone needs nothing."""
        script = self.script.replace("--paginate ", "")
        self.assertNotEqual(script, self.script)
        result = self._run("workflow_dispatch", script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._writes(), [])
        self.assertIn("0 change(s)", result.stdout)

    def test_sweep_continues_past_a_label_another_run_already_removed(self):
        """A sweep reads every page before it writes; a 404 on the DELETE means the rule already holds there."""
        result = self._run("workflow_dispatch", delete_404="3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._writes(), [("POST", 1), ("DELETE", 3), ("POST", 7), ("DELETE", 8)])
        self.assertIn(f"remove: {_LABEL} on #3 -- already gone", result.stdout)
        self.assertIn("3 change(s), 1 skipped: already gone", result.stdout)

    def test_sweep_continues_past_an_issue_deleted_since_the_read(self):
        """The add side of the same window: the issue itself is gone, so there is nothing to label and the rest of the sweep still runs."""
        result = self._run("workflow_dispatch", missing="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._writes(), [("POST", 1), ("DELETE", 3), ("POST", 7), ("DELETE", 8)])
        self.assertIn(f"add: {_LABEL} on #1 -- issue gone", result.stdout)
        self.assertIn("3 change(s), 1 skipped: already gone", result.stdout)

    def test_dispatch_dry_run_writes_nothing(self):
        result = self._run("workflow_dispatch", dry_run="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._writes(), [])
        for number in _EXPECTED_ADDS:
            self.assertIn(f"would add {_LABEL} on #{number}", result.stdout)
        for number in _EXPECTED_REMOVES:
            self.assertIn(f"would remove {_LABEL} on #{number}", result.stdout)

    def test_event_touches_only_its_issue(self):
        result = self._run("issues", issue_number="3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._writes(), [("DELETE", 3)])

    def test_event_on_a_pull_request_or_closed_issue_is_a_no_op(self):
        for number in ("5", "6"):
            with self.subTest(number=number):
                result = self._run("issues", issue_number=number)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self._writes(), [])
                self.assertIn("0 change(s)", result.stdout)

    def test_event_on_a_deleted_issue_is_a_no_op(self):
        """An issue deleted before its `opened` run starts answers the read with 404; that is not a failure."""
        result = self._run("issues", issue_number="1", missing="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._writes(), [])
        self.assertIn("issue #1 no longer exists", result.stdout)
        self.assertIn("0 change(s)", result.stdout)

    def test_a_failed_write_fails_the_step(self):
        """A label the API refused for any other reason is a red run, not a quiet drift."""
        result = self._run("issues", issue_number="1", fail_writes="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("(HTTP 500)", result.stderr)
        self.assertNotIn(f"add: {_LABEL}", result.stdout)

    def test_unknown_event_fails(self):
        result = self._run("push")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected event", result.stderr)


if __name__ == "__main__":
    unittest.main()
