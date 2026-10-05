"""The in-job repository reset (#2260): hack/ci_reset_agent_pulls.py and the
step in hack/ci-eval-pr.sh that runs it.

What has to hold, against the code that ships (the helper imported, the shell
lifted out of the script):

* ownership is the author and the head repository, never the branch name: a
  bot's pull request on `fix-payments-api-oom` goes, a human's on
  `platform-agent/anything` stays, and so does its branch;
* an audit pull request is labelled `audit:stale-closed` before its close, and
  a label that will not stick leaves it open rather than closed unlabelled;
* every branch but the default goes, unless an open pull request that stays
  has it as head;
* the read-back is the verdict: a repository that still lists an agent pull
  request or a branch afterwards exits non-zero, and the shell step then
  returns 1 so the unit does not run;
* the mint is narrowed to the one repository and the three writes, the token
  rides in the environment, and the record lands beside the artifacts.
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
HELPER = REPO_ROOT / "hack" / "ci_reset_agent_pulls.py"
SWEEP = REPO_ROOT / "hack" / "ci_sweep_agent_pulls.py"
AUDIT_REPORT = REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "audit_report.py"

# The helper imports its sibling by bare name, as the script's own invocation
# (`python3 hack/ci_reset_agent_pulls.py`) resolves it.
sys.path.insert(0, str(REPO_ROOT / "hack"))
_spec = importlib.util.spec_from_file_location("ci_reset_agent_pulls", HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)

REPO = "gke-agentic/kube-agents-evals-2-infra"
PROJECT = "kube-agents-evals-2"
BOT = "kube-agents-evals-token-minter[bot]"
BUILD = "2102186223282950144"


def pull(number, branch="fix-payments-api-oom", author=BOT, head_repo=REPO, labels=()):
    return {
        "number": number,
        "user": {"login": author},
        "head": {"ref": branch, "repo": {"full_name": head_repo} if head_repo else None},
        "labels": [{"name": name} for name in labels],
    }


def _http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "reason", {}, io.BytesIO(b""))


class FakeGitHub:
    """A repository that changes as it is written to, so the read-back reads
    the result of the writes. Records every call in order."""

    def __init__(self, pulls=(), branches=(), default="main", fail=None, stubborn=()):
        self.open = {p["number"]: p for p in pulls}
        self.branches = [default, *branches]
        self.default = default
        self.calls = []
        # {("PATCH", number) | ("POST", number) | ("DELETE", branch): exception}
        self.fail = fail or {}
        # Branches whose delete is answered 204 and which stay listed anyway.
        self.stubborn = set(stubborn)

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, body))
        assert token == "tok"
        if method == "GET" and path == f"/repos/{REPO}":
            return {"default_branch": self.default}
        if method == "GET" and path.startswith(f"/repos/{REPO}/pulls?"):
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return sorted(self.open.values(), key=lambda p: p["number"]) if page == 1 else []
        if method == "GET" and path.startswith(f"/repos/{REPO}/branches?"):
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return [{"name": n} for n in self.branches] if page == 1 else []
        if method == "POST" and path.endswith("/labels"):
            number = int(path.rsplit("/", 2)[1])
            if ("POST", number) in self.fail:
                raise self.fail[("POST", number)]
            self.open[number].setdefault("labels", []).append({"name": body["labels"][0]})
            return []
        if method == "PATCH":
            number = int(path.rsplit("/", 1)[1])
            if ("PATCH", number) in self.fail:
                raise self.fail[("PATCH", number)]
            assert body == {"state": "closed"}
            del self.open[number]
            return {}
        if method == "DELETE":
            branch = urllib.parse.unquote(path.split("/git/refs/heads/", 1)[1])
            if ("DELETE", branch) in self.fail:
                raise self.fail[("DELETE", branch)]
            if branch not in self.branches:
                raise _http_error(422)
            if branch not in self.stubborn:
                self.branches.remove(branch)
            return None
        raise AssertionError(f"unexpected call {method} {path}")

    def keys(self, method):
        return [path for m, path, _ in self.calls if m == method]


import urllib.parse  # noqa: E402  (after the module load, like the helper's own)


def run_reset(github, dry_run=False, repo=REPO, project=PROJECT):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(helper.ledgers, "api", github), mock.patch.object(helper, "pause", lambda s: None), redirect_stdout(out), redirect_stderr(err):
        record = helper.reset(repo, project, BUILD, "before pdb-remediation-pr rep 2", "tok", dry_run)
    return record, out.getvalue(), err.getvalue()


class OwnershipTest(unittest.TestCase):
    def test_a_bot_pull_request_on_any_branch_name_is_the_agents(self):
        self.assertTrue(helper.is_agent_pull_request(pull(1, branch="fix-payments-api-oom"), REPO))
        self.assertTrue(helper.is_agent_pull_request(pull(1, branch="platform-agent/fix-x"), REPO))

    def test_a_human_or_a_fork_is_not(self):
        self.assertFalse(helper.is_agent_pull_request(pull(1, author="a-human", branch="platform-agent/theirs"), REPO))
        self.assertFalse(helper.is_agent_pull_request(pull(1, head_repo="someone/kube-agents-evals-2-infra"), REPO))
        self.assertFalse(helper.is_agent_pull_request(pull(1, head_repo=None), REPO))

    def test_the_labels_are_the_sweeps_and_the_audits(self):
        sweep = SWEEP.read_text(encoding="utf-8")
        audit = AUDIT_REPORT.read_text(encoding="utf-8")
        self.assertIn(f'STALE_CLOSED_LABEL = "{helper.STALE_CLOSED_LABEL}"', sweep)
        self.assertIn(f'AUDIT_REMEDIATION_LABEL = "{helper.AUDIT_REMEDIATION_LABEL}"', sweep)
        self.assertIn(f'STALE_CLOSED_LABEL = "{helper.STALE_CLOSED_LABEL}"', audit)
        self.assertIn(f'"{helper.AUDIT_REMEDIATION_LABEL}",', audit)


class RepositoryGuardTest(unittest.TestCase):
    def test_a_mismatch_is_refused_before_any_call(self):
        github = FakeGitHub()
        with self.assertRaises(helper.ResetError):
            run_reset(github, project="kube-agents-evals-3")
        self.assertEqual(github.calls, [])


class ResetTest(unittest.TestCase):
    def test_the_agents_pull_requests_close_and_a_humans_stays_with_its_branch(self):
        github = FakeGitHub(
            pulls=[pull(1), pull(2, branch="platform-agent/theirs", author="a-human"), pull(3, branch="platform-agent/fix-x")],
            branches=["fix-payments-api-oom", "platform-agent/theirs", "platform-agent/fix-x", "feature/add-seeded-c"],
        )
        record, out, err = run_reset(github)
        self.assertEqual(record["closed"], [1, 3])
        self.assertEqual(record["kept_open"], [2])
        self.assertEqual(sorted(record["deleted"]), ["feature/add-seeded-c", "fix-payments-api-oom", "platform-agent/fix-x"])
        self.assertEqual(record["kept_branches"], ["platform-agent/theirs"])
        self.assertEqual(github.branches, ["main", "platform-agent/theirs"])
        self.assertTrue(record["clean"], (out, err))
        self.assertIn("#2 left open, not the agent's", out)

    def test_an_audit_pull_request_is_labelled_before_it_closes(self):
        github = FakeGitHub(pulls=[pull(7, labels=["agent:audit", "audit:remediation"]), pull(8)], branches=["fix-payments-api-oom"])
        record, _, _ = run_reset(github)
        keys = [(m, p) for m, p, _ in github.calls]
        label = keys.index(("POST", f"/repos/{REPO}/issues/7/labels"))
        close = keys.index(("PATCH", f"/repos/{REPO}/pulls/7"))
        self.assertLess(label, close)
        self.assertEqual(record["labelled"], [7])
        self.assertNotIn(("POST", f"/repos/{REPO}/issues/8/labels"), keys)
        self.assertEqual([b for m, p, b in github.calls if m == "POST"], [{"labels": [helper.STALE_CLOSED_LABEL]}])

    def test_a_label_that_will_not_stick_leaves_the_pull_request_open(self):
        github = FakeGitHub(pulls=[pull(7, labels=["audit:remediation"])], branches=["fix-payments-api-oom"], fail={("POST", 7): _http_error(409)})
        record, _, err = run_reset(github)
        self.assertEqual(record["unclosed"], [7])
        self.assertEqual(github.keys("PATCH"), [])
        self.assertEqual(github.keys("DELETE"), [], "its branch is not deleted from under it")
        self.assertFalse(record["clean"])
        self.assertIn("#7 did not close", err)

    def test_a_close_that_fails_does_not_stop_the_rest(self):
        github = FakeGitHub(pulls=[pull(1), pull(2, branch="b2")], branches=["fix-payments-api-oom", "b2"], fail={("PATCH", 1): _http_error(403)})
        record, _, _ = run_reset(github)
        self.assertEqual((record["unclosed"], record["closed"]), ([1], [2]))
        self.assertEqual(record["deleted"], ["b2"])
        self.assertFalse(record["clean"])

    def test_a_fork_pull_requests_head_name_shields_no_branch_here(self):
        # A human's pull request from a fork named like a leftover branch in
        # this repository keeps nothing: only a head in the repository itself
        # is one the branch pass could pull from under a pull request.
        github = FakeGitHub(
            pulls=[pull(9, author="a-human", branch="fix-payments-api-oom", head_repo="someone/kube-agents-evals-2-infra")],
            branches=["fix-payments-api-oom"],
        )
        record, _, _ = run_reset(github)
        self.assertEqual(record["kept_open"], [9])
        self.assertEqual(record["deleted"], ["fix-payments-api-oom"])
        self.assertEqual(record["kept_branches"], [])
        self.assertTrue(record["clean"])

    def test_a_branch_github_refuses_to_delete_leaves_the_repository_not_clean(self):
        # Whatever the reason -- a limit the retries outlasted, a protection,
        # a reach the mint did not give -- a branch left is a branch the next
        # agent would start from, so the unit does not run on it.
        github = FakeGitHub(pulls=[pull(1)], branches=["fix-payments-api-oom", "release"], fail={("DELETE", "release"): _http_error(403)})
        record, out, err = run_reset(github)
        self.assertEqual(record["undeleted"], ["release"])
        self.assertEqual(record["deleted"], ["fix-payments-api-oom"])
        self.assertFalse(record["clean"])
        self.assertIn("branch release was not deleted", err)

    def test_the_base_branch_of_a_humans_open_pull_request_stays(self):
        # GitHub closes a pull request whose base branch is deleted, which
        # would make the reset close what it promised to keep and then fail
        # its own read-back on the head branch left behind.
        github = FakeGitHub(
            pulls=[dict(pull(9, author="a-human", branch="feature/x"), base={"ref": "release/1.2"})],
            branches=["feature/x", "release/1.2", "fix-payments-api-oom"],
        )
        record, _, _ = run_reset(github)
        self.assertEqual(sorted(record["kept_branches"]), ["feature/x", "release/1.2"])
        self.assertEqual(record["deleted"], ["fix-payments-api-oom"])
        self.assertTrue(record["clean"])

    def test_every_branch_but_the_default_goes_whatever_its_name(self):
        github = FakeGitHub(branches=["fix-payments-api-oom", "feature/add-seeded-c", "platform-agent/orphan"], default="main")
        record, _, _ = run_reset(github)
        self.assertEqual(sorted(record["deleted"]), ["feature/add-seeded-c", "fix-payments-api-oom", "platform-agent/orphan"])
        self.assertEqual(github.branches, ["main"])
        self.assertTrue(record["clean"])

    def test_the_default_branch_is_read_not_assumed(self):
        github = FakeGitHub(branches=["main"], default="main-2")
        record, _, _ = run_reset(github)
        self.assertEqual(record["deleted"], ["main"])
        self.assertEqual(github.branches, ["main-2"])

    def test_a_branch_already_gone_is_not_a_failure(self):
        github = FakeGitHub(branches=["b"], fail={("DELETE", "b"): _http_error(422)})
        github.stubborn = set()
        record, _, _ = run_reset(github)
        self.assertEqual(record["undeleted"], [])

    def test_a_branch_that_survives_its_delete_fails_the_read_back(self):
        github = FakeGitHub(branches=["b"], stubborn=["b"])
        record, out, _ = run_reset(github)
        self.assertEqual(record["deleted"], ["b"])
        self.assertEqual(record["branches_after"], 1)
        self.assertFalse(record["clean"])
        self.assertIn("1 branch(es) remain", out)

    def test_a_clean_repository_is_a_quiet_clean(self):
        record, out, err = run_reset(FakeGitHub())
        self.assertTrue(record["clean"])
        self.assertEqual((record["open_before"], record["branches_before"]), (0, 0))
        self.assertEqual(err, "")

    def test_dry_run_writes_nothing(self):
        github = FakeGitHub(pulls=[pull(1, labels=["audit:remediation"])], branches=["fix-payments-api-oom"])
        record, out, _ = run_reset(github, dry_run=True)
        self.assertEqual([m for m, _, _ in github.calls if m != "GET"], [])
        self.assertEqual(record["open_before"], 1)
        self.assertIn("would close 1 pull request(s) and delete 1 branch(es)", out)

    def test_every_write_is_paced(self):
        pauses = []
        github = FakeGitHub(pulls=[pull(1, labels=["audit:remediation"])], branches=["fix-payments-api-oom", "b"])
        with mock.patch.object(helper.ledgers, "api", github), mock.patch.object(helper, "pause", pauses.append), redirect_stdout(io.StringIO()):
            helper.reset(REPO, PROJECT, BUILD, "lease", "tok", False)
        # Label, close, two deletes: four writes, four pauses.
        self.assertEqual(pauses, [helper.WRITE_PAUSE_SECONDS] * 4)


class RetryTest(unittest.TestCase):
    """A transient answer is tried again; a refusal is not. The reset fails
    closed, so without this one GitHub hiccup would grade a repetition MISSING."""

    def flaky(self, failures, github):
        remaining = list(failures)

        def api(method, path, token, body=None):
            if remaining and (method, path.split("?")[0]) == remaining[0][0]:
                exc = remaining.pop(0)[1]
                raise exc
            return github(method, path, token, body)

        return api

    def run_with(self, api):
        pauses = []
        with mock.patch.object(helper.ledgers, "api", api), mock.patch.object(helper, "pause", pauses.append), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            record = helper.reset(REPO, PROJECT, BUILD, "lease", "tok", False)
        return record, pauses

    def test_a_listing_that_answers_5xx_twice_then_lists_is_clean(self):
        github = FakeGitHub(pulls=[pull(1)], branches=["fix-payments-api-oom"])
        listing = ("GET", f"/repos/{REPO}/pulls")
        record, pauses = self.run_with(self.flaky([(listing, _http_error(502)), (listing, _http_error(503))], github))
        self.assertTrue(record["clean"])
        self.assertEqual(pauses[:2], list(helper.RETRY_DELAYS_SECONDS))

    def test_a_close_that_drops_once_then_closes_is_closed(self):
        github = FakeGitHub(pulls=[pull(1)], branches=["fix-payments-api-oom"])
        record, _ = self.run_with(self.flaky([(("PATCH", f"/repos/{REPO}/pulls/1"), OSError("connection reset"))], github))
        self.assertEqual((record["closed"], record["unclosed"]), ([1], []))

    def test_a_response_cut_short_is_tried_again(self):
        # http.client's own faults are not OSErrors and urllib passes them on
        # unwrapped; a close that meets one is retried like a dropped socket.
        github = FakeGitHub(pulls=[pull(1)], branches=["fix-payments-api-oom"])
        record, pauses = self.run_with(self.flaky([(("PATCH", f"/repos/{REPO}/pulls/1"), http.client.IncompleteRead(b""))], github))
        self.assertEqual((record["closed"], record["unclosed"]), ([1], []))
        self.assertIn(helper.RETRY_DELAYS_SECONDS[0], pauses)

    def test_a_403_github_marks_as_its_limit_waits_what_it_asks(self):
        github = FakeGitHub(pulls=[pull(1)], branches=["fix-payments-api-oom"])
        limited = urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden", {"Retry-After": "7"}, io.BytesIO(b""))
        record, pauses = self.run_with(self.flaky([(("PATCH", f"/repos/{REPO}/pulls/1"), limited)], github))
        self.assertEqual(record["closed"], [1])
        self.assertIn(7, pauses)
        # A 403 that carries none of the limit's marks is a refusal.
        self.assertFalse(helper.is_rate_limited(_http_error(403)))
        self.assertTrue(helper.is_rate_limited(urllib.error.HTTPError("u", 403, "F", {"X-RateLimit-Remaining": "0"}, io.BytesIO(b""))))
        self.assertTrue(helper.is_rate_limited(urllib.error.HTTPError("u", 403, "F", {}, io.BytesIO(b'{"message":"You have exceeded a secondary rate limit"}'))))
        self.assertEqual(helper.retry_after(urllib.error.HTTPError("u", 429, "L", {"Retry-After": "999"}, io.BytesIO(b""))), helper.sweep.RETRY_AFTER_MAX_SECONDS)
        self.assertEqual(helper.retry_after(_http_error(429)), helper.sweep.RETRY_AFTER_DEFAULT_SECONDS)

    def test_a_403_marked_only_in_its_body_waits_the_default(self):
        # The body is read once and kept on the exception, so the mark is
        # still there when the wait is chosen: GitHub's shape with no header.
        github = FakeGitHub(pulls=[pull(1)], branches=["fix-payments-api-oom"])
        limited = urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden", {}, io.BytesIO(b'{"message":"You have exceeded a secondary rate limit. Please wait."}'))
        record, pauses = self.run_with(self.flaky([(("PATCH", f"/repos/{REPO}/pulls/1"), limited)], github))
        self.assertEqual(record["closed"], [1])
        self.assertIn(helper.sweep.RETRY_AFTER_DEFAULT_SECONDS, pauses)
        self.assertNotIn(helper.RETRY_DELAYS_SECONDS[0], pauses)

    def test_a_refusal_is_not_tried_again(self):
        github = FakeGitHub(pulls=[pull(1)])
        record, pauses = self.run_with(self.flaky([(("PATCH", f"/repos/{REPO}/pulls/1"), _http_error(403))], github))
        self.assertEqual(record["unclosed"], [1])
        self.assertEqual([m for m, p, _ in github.calls if m == "PATCH"], [], "the refused call was made once, to the fake's predecessor")
        self.assertEqual(pauses, [helper.WRITE_PAUSE_SECONDS])

    def test_three_transient_answers_surface_as_the_fault(self):
        listing = ("GET", f"/repos/{REPO}/pulls")
        api = self.flaky([(listing, _http_error(502))] * 3, FakeGitHub())
        with mock.patch.object(helper.ledgers, "api", api), mock.patch.object(helper, "pause", lambda s: None), redirect_stderr(io.StringIO()):
            with self.assertRaises(urllib.error.HTTPError):
                helper.reset(REPO, PROJECT, BUILD, "lease", "tok", False)


class MainTest(unittest.TestCase):
    def run_main(self, github, *args, token="tok"):
        out, err = io.StringIO(), io.StringIO()
        env = {helper.TOKEN_ENV: token} if token else {}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(helper.ledgers, "api", github), mock.patch.object(helper, "pause", lambda s: None), redirect_stdout(out), redirect_stderr(err):
            if not token:
                os.environ.pop(helper.TOKEN_ENV, None)
            rc = helper.main(["--repo", REPO, "--project", PROJECT, "--build", BUILD, *args])
        return rc, out.getvalue(), err.getvalue()

    def test_clean_is_zero_and_not_clean_is_one(self):
        self.assertEqual(self.run_main(FakeGitHub(pulls=[pull(1)], branches=["fix-payments-api-oom"]))[0], 0)
        self.assertEqual(self.run_main(FakeGitHub(branches=["b"], stubborn=["b"]))[0], 1)
        self.assertEqual(self.run_main(FakeGitHub(pulls=[pull(1)], fail={("PATCH", 1): _http_error(403)}))[0], 1)

    def test_the_record_is_written_with_the_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "agent-pulls-reset" / "lease.json"
            rc, _, _ = self.run_main(FakeGitHub(pulls=[pull(1)], branches=["fix-payments-api-oom"]), "--scope", "lease", "--record", str(path))
            self.assertEqual(rc, 0)
            record = json.loads(path.read_text())
        self.assertEqual((record["scope"], record["closed"], record["deleted"], record["clean"]), ("lease", [1], ["fix-payments-api-oom"], True))
        self.assertEqual(record["schema_version"], helper.RECORD_SCHEMA_VERSION)

    def test_a_faulted_reset_still_writes_its_record(self):
        # The shell step names the record as evidence when the helper exits
        # non-zero, so the reset that faulted must have one too.
        def denied(method, path, token, body=None):
            raise _http_error(403)

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "lease.json"
            rc, _, _ = self.run_main(denied, "--scope", "lease", "--record", str(path))
            self.assertEqual(rc, 1)
            record = json.loads(path.read_text())
        self.assertFalse(record["clean"])
        self.assertIn("403", record["error"])
        self.assertEqual(record["scope"], "lease")

    def test_a_missing_token_a_guard_and_a_fault_each_have_their_code(self):
        self.assertEqual(self.run_main(FakeGitHub(), token="")[0], 2)
        with mock.patch.dict(os.environ, {helper.TOKEN_ENV: "tok"}), redirect_stderr(io.StringIO()):
            self.assertEqual(helper.main(["--repo", "gke-agentic/other-infra", "--project", PROJECT, "--build", BUILD]), 2)

        def denied(method, path, token, body=None):
            raise _http_error(403)

        self.assertEqual(self.run_main(denied)[0], 1)

        def unreachable(method, path, token, body=None):
            raise OSError("connection refused")

        rc, _, err = self.run_main(unreachable)
        self.assertEqual(rc, 1)
        self.assertIn("could not reach api.github.com", err)

        def cut_short(method, path, token, body=None):
            raise http.client.IncompleteRead(b"")

        rc, _, err = self.run_main(cut_short)
        self.assertEqual(rc, 1, "an http.client fault is a reported fault, never a traceback")
        self.assertIn("IncompleteRead", err)

    def test_dry_run_is_zero_whatever_it_finds(self):
        self.assertEqual(self.run_main(FakeGitHub(pulls=[pull(1)]), "--dry-run")[0], 0)


# --- the shell half, lifted out of hack/ci-eval-pr.sh ------------------------


def lifted(name: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{name}() not found in {SCRIPT}")
    return match.group(0)


def lifted_line(pattern: str) -> str:
    match = re.search(pattern, SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, pattern
    return match.group(0)


def run_bash(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -uo pipefail\n" + body],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


STUB_HELPER = textwrap.dedent(
    """\
    import json, os, sys
    print("HELPER argv=" + json.dumps(sys.argv[1:]))
    print("HELPER token=" + os.environ.get("AGENT_PULLS_RESET_TOKEN", ""))
    sys.exit(int(os.environ.get("HELPER_RC", "0")))
    """
)


class ResetStepTest(unittest.TestCase):
    """reset_agent_pulls with the mint and the helper stubbed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        (self.dir / "ci_reset_agent_pulls.py").write_text(STUB_HELPER)
        self.body_file = self.dir / "mint-body"

    def run_step(self, label, key_file="/etc/ledger-app-key/key.pem", repo=REPO, mint_rc=None, helper_rc=0):
        mint = "\n".join(
            [
                "_ledger_token_mint() {",
                f'  printf "%s" "${{LEDGER_MINT_BODY:-}}" > "{self.body_file}"',
                "  echo minted >&2",
                (f"  return {mint_rc}" if mint_rc is not None else '  echo "tok-write 2026-10-01T23:00:00Z"'),
                "}",
                "sleep() { :; }",
            ]
        )
        body = "\n".join(
            [
                lifted_line(r"^LEDGER_MINT_RETRYABLE=\d+$"),
                lifted_line(r"^LEDGER_RESET_MINT_ATTEMPTS=\d+$"),
                lifted_line(r"^LEDGER_RESET_MINT_RETRY_DELAY=\d+$"),
                lifted_line(r"^AGENT_PULLS_RESET_PERMISSIONS=.*$"),
                f'EVAL_LEDGER_APP_ID=4739812; EVAL_LEDGER_APP_KEY_FILE="{key_file}"',
                f'EVAL_LEDGER_REPO="{repo}"; PROJECT_ID="{PROJECT}"; BUILD_ID={BUILD}',
                f'SCRIPT_DIR="{self.dir}"; ARTIFACT_DIR="{self.dir}/artifacts"',
                mint,
                lifted("ledger_reset_token"),
                lifted("reset_agent_pulls"),
                f'reset_agent_pulls "{label}"',
                'echo "RC=$?"',
            ]
        )
        return run_bash(body, {"HELPER_RC": str(helper_rc)})

    def test_the_mint_is_narrowed_to_the_repository_and_the_three_writes(self):
        result = self.run_step("pdb-remediation-pr rep 2")
        self.assertIn("RC=0", result.stdout, result.stderr)
        # Pinned as a literal: a test that read the body out of the script
        # would pass whatever the script asked for.
        self.assertEqual(
            json.loads(self.body_file.read_text()),
            {"repositories": ["kube-agents-evals-2-infra"], "permissions": {"pull_requests": "write", "contents": "write", "issues": "write"}},
        )

    def test_the_helper_is_told_the_scope_and_the_record_and_never_the_token(self):
        result = self.run_step("pdb-remediation-pr rep 2")
        argv = json.loads(re.search(r"HELPER argv=(.*)", result.stdout).group(1))
        self.assertEqual(
            argv,
            ["--repo", REPO, "--project", PROJECT, "--build", BUILD, "--scope", "pdb-remediation-pr rep 2", "--record", f"{self.dir}/artifacts/agent-pulls-reset/pdb-remediation-pr_rep_2.json"],
        )
        self.assertNotIn("tok-write", " ".join(argv))
        self.assertIn("HELPER token=tok-write", result.stdout)
        self.assertIn("Agent pulls reset (pdb-remediation-pr rep 2): HELPER argv=", result.stdout)
        self.assertNotIn("BENCH_GITHUB_TOKEN", lifted("reset_agent_pulls"))

    def test_pat_mode_and_an_unmapped_project_skip_out_loud(self):
        for kwargs, needle in (({"key_file": ""}, "EVAL_LEDGER_APP_KEY_FILE is unset"), ({"repo": ""}, "maps to no GitOps repository")):
            with self.subTest(**kwargs):
                result = self.run_step("lease", **kwargs)
                self.assertIn(needle, result.stdout)
                self.assertNotIn("HELPER", result.stdout)
                self.assertIn("RC=0", result.stdout)

    def test_a_refused_mint_fails_closed_and_names_the_grant(self):
        result = self.run_step("pdb-remediation-pr rep 2", mint_rc=1)
        self.assertIn("could not mint pull_requests: write, contents: write and issues: write", result.stderr)
        self.assertIn("does not run on a repository this could not clean", result.stderr)
        self.assertNotIn("HELPER", result.stdout)
        self.assertIn("RC=1", result.stdout)

    def test_a_helper_that_finds_the_repository_not_clean_fails_closed(self):
        result = self.run_step("lease", helper_rc=1)
        self.assertIn("WARNING: Agent pulls reset (lease): the helper exited 1; the repository is not clean", result.stderr)
        self.assertIn("RC=1", result.stdout)


class CallSiteTest(unittest.TestCase):
    """Where the step runs. run_one_unit cannot be executed here (it runs
    devops-bench), so its wiring is pinned on the text: the reset sits after
    the ledger reset, is gated on unit_phase, and a failure releases every
    lock the unit holds and returns before devops-bench."""

    def test_the_lease_time_reset_follows_the_ledger_reset(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIsNotNone(re.search(r'^reset_audit_ledgers "lease"\nreset_agent_pulls "lease" \|\| echo "WARNING', src, re.M), "lease-time call")

    def test_the_unit_reset_is_gated_on_the_writer_phase_and_fails_closed(self):
        unit = lifted("run_one_unit")
        gate = re.search(
            r'if \[ "\$\(unit_phase "\$\{name\}"\)" = "1" \] && ! reset_agent_pulls "\$\{name\} rep \$\{rep\}"; then\n(.*?)\n    return 0\n  fi',
            unit,
            re.DOTALL,
        )
        self.assertIsNotNone(gate, "the gated call in run_one_unit")
        body = gate.group(1)
        for lock in ("lock-stream-${audit_id}", "lock-infra", "lock-task-${name}"):
            self.assertIn(f'lock_release "${{STATE_DIR}}/{lock}"', body)
        self.assertLess(unit.index('reset_audit_ledgers "${name} rep ${rep}"'), unit.index("reset_agent_pulls"), "after the ledger reset")
        self.assertLess(unit.index("reset_agent_pulls"), unit.index("uv run devops-bench"), "before devops-bench")


if __name__ == "__main__":
    unittest.main()
