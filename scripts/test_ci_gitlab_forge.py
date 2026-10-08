"""The GitLab half of the eval's repository hygiene (hack/ci_gitlab_forge.py,
kube-agents#2394), and the `--forge gitlab` paths of the three callers.

What has to hold, checked against the code that ships (the modules imported,
the shell lifted out of hack/ci-eval-pr.sh):

  - never any project but the leased one's: `gke-agentic/<PROJECT_ID>-infra`
    or a refusal before any call;
  - a ledger is the GitHub rule with the bot's login for the `[bot]` suffix,
    closed with a note that opens with the shared RESET_MARKER, then
    `state_event: close`; nothing else closes;
  - a merge request goes when the bot authored it from a branch in the project
    itself; an audit's is labelled before it is closed; every branch but the
    default goes unless an open merge request that stays needs it; the
    read-back decides `clean`, in the GitHub reset's record shape;
  - the sweep's GitLab pass reads the pool's pair through gcloud, never argv,
    walks the GitLab mapping, writes its own report, and fails the run when a
    token is inside the warning window, naming it;
  - the eval script under EVAL_FORGE=gitlab reads the pair once, exports what
    the bench reads with the GitHub token unset, hands the agent token and
    `--forge gitlab` to the resets, and refuses a forge it does not know; its
    secret names equal the deploy's.
"""

import datetime
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
HACK = REPO_ROOT / "hack"
SCRIPT = HACK / "ci-eval-pr.sh"
CI_DEPLOY = HACK / "ci-deploy.sh"
if str(HACK) not in sys.path:
    sys.path.insert(0, str(HACK))
import ci_gitlab_forge as gitlab  # noqa: E402
import ci_reset_agent_pulls as pulls  # noqa: E402
import ci_reset_audit_ledgers as ledgers  # noqa: E402
import ci_sweep_agent_pulls as sweeper  # noqa: E402

PATH = "gke-agentic/kube-agents-evals-2-infra"
PROJECT = "kube-agents-evals-2"
BOT = "kube-agents-eval-bot"
PROJECT_ID = 144080700
ENC = "gke-agentic%2Fkube-agents-evals-2-infra"


def issue(iid, audit_id="compliance-audit", title=None, author=BOT, labels=None):
    names = ["agent:audit", f"audit:{audit_id}", "severity:major"] if labels is None else labels
    return {
        "iid": iid,
        "title": title if title is not None else f"[audit] Security & RBAC Posture Audit — {iid} findings",
        "author": {"username": author},
        "labels": names,
    }


def mr(iid, author=BOT, source="platform-agent/fix", source_project=PROJECT_ID, target="main", labels=()):
    return {
        "iid": iid,
        "author": {"username": author},
        "source_branch": source,
        "source_project_id": source_project,
        "target_branch": target,
        "labels": list(labels),
    }


class FakeGitLab:
    """Serves listings by path and records every call, in order."""

    def __init__(self, issues=(), mrs=(), branches=("main",), default="main", fail=(), login=BOT):
        self.login = login
        self.issues = list(issues)
        self.mrs = list(mrs)
        self.branches = list(branches)
        self.default = default
        self.calls = []
        self.fail = set(fail)  # (method, path-suffix) pairs that raise 403

    def __call__(self, method, path, token, body=None, host=gitlab.DEFAULT_HOST):
        self.calls.append((method, path, body))
        for m, suffix in self.fail:
            if method == m and path.split("?")[0].endswith(suffix):
                raise urllib.error.HTTPError(path, 403, "Forbidden", {}, io.BytesIO(b""))
        bare = path.split("?")[0]
        page = int(re.search(r"[?&]page=(\d+)", path).group(1)) if "page=" in path else 1
        if method == "GET":
            if bare == f"/projects/{ENC}":
                return {"id": PROJECT_ID, "default_branch": self.default}
            if bare == f"/projects/{ENC}/issues":
                return self.issues if page == 1 else []
            if bare == f"/projects/{ENC}/merge_requests":
                return [m for m in self.mrs if m.get("state", "opened") == "opened"] if page == 1 else []
            if bare == f"/projects/{ENC}/repository/branches":
                return [{"name": n} for n in self.branches] if page == 1 else []
            if bare == "/user":
                return {"username": self.login}
            if bare == gitlab.TOKEN_SELF_PATH:
                return {"name": "kube-agents-evals-agent", "scopes": ["api", "write_repository"], "expires_at": "2027-10-05", "active": True, "revoked": False}
            raise AssertionError(f"unexpected GET {path}")
        if method == "PUT" and "/merge_requests/" in bare:
            iid = int(bare.rsplit("/", 1)[1])
            for m in self.mrs:
                if m["iid"] == iid:
                    if body.get("state_event") == "close":
                        m["state"] = "closed"
                    if body.get("add_labels"):
                        m["labels"] = list(m.get("labels", [])) + [body["add_labels"]]
            return {}
        if method == "PUT" and "/issues/" in bare:
            iid = int(bare.rsplit("/", 1)[1])
            self.issues = [i for i in self.issues if i["iid"] != iid]
            return {}
        if method == "DELETE" and "/repository/branches/" in bare:
            name = urllib.parse.unquote(bare.rsplit("/", 1)[1])
            if name not in self.branches:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO(b""))
            self.branches.remove(name)
            return None
        return {}


import urllib.parse  # noqa: E402  (used by FakeGitLab)


class ProjectGuardTest(unittest.TestCase):
    def test_the_leased_projects_own_path_passes(self):
        gitlab.expected_project(PATH, PROJECT)

    def test_anything_else_is_refused_before_any_call(self):
        for path, project in (
            (PATH, ""),
            ("gke-agentic/kube-agents-evals-3-infra", PROJECT),
            ("other-group/kube-agents-evals-2-infra", PROJECT),
            ("gke-agentic/sub/kube-agents-evals-2-infra", PROJECT),
            ("kube-agents-evals-2-infra", PROJECT),
        ):
            with self.subTest(path=path, project=project):
                with self.assertRaises(gitlab.ResetError):
                    gitlab.expected_project(path, project)

    def test_the_path_is_encoded_whole_as_gitlabs_id(self):
        self.assertEqual(gitlab.encoded(PATH), ENC)


class LedgerSelectionTest(unittest.TestCase):
    def test_a_ledger_is_the_github_rule_with_the_bot_login(self):
        self.assertIsNone(gitlab.not_a_ledger_because(issue(1), None))
        self.assertIsNone(gitlab.not_a_ledger_because(issue(1), "compliance-audit"))

    def test_each_missing_condition_is_named(self):
        cases = {
            "no agent:audit label": issue(1, labels=["audit:compliance-audit"]),
            "no audit:obtainability-audit label": issue(1),
            "title does not start with '[audit] '": issue(1, title="Security posture"),
            "author someone is not kube-agents-eval-bot": issue(1, author="someone"),
        }
        for reason, item in cases.items():
            with self.subTest(reason=reason):
                audit = "obtainability-audit" if "obtainability" in reason else None
                self.assertEqual(gitlab.not_a_ledger_because(item, audit), reason)

    def test_labels_as_dicts_are_read_too(self):
        item = issue(1)
        item["labels"] = [{"name": n} for n in item["labels"]]
        self.assertIsNone(gitlab.not_a_ledger_because(item, None))


class LedgerResetTest(unittest.TestCase):
    def _reset(self, fake, audit_id=None, dry_run=False):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gitlab, "api", fake), redirect_stdout(out), redirect_stderr(err):
            unclosed = gitlab.reset_ledgers(PATH, PROJECT, "2102186223282950144", "tok", audit_id, dry_run)
        return unclosed, out.getvalue(), err.getvalue()

    def test_ledgers_close_with_the_marked_note_first(self):
        fake = FakeGitLab(issues=[issue(3), issue(1), issue(2, author="human")])
        unclosed, out, _ = self._reset(fake)
        self.assertEqual(unclosed, 0)
        writes = [(m, p, b) for m, p, b in fake.calls if m != "GET"]
        self.assertEqual(
            [(m, p.rsplit("/", 2)[-2:], sorted(b)) for m, p, b in writes],
            [
                ("POST", ["1", "notes"], ["body"]),
                ("PUT", ["issues", "1"], ["state_event"]),
                ("POST", ["3", "notes"], ["body"]),
                ("PUT", ["issues", "3"], ["state_event"]),
            ],
        )
        self.assertTrue(writes[0][2]["body"].startswith(ledgers.RESET_MARKER))
        self.assertEqual(writes[1][2], {"state_event": "close"})
        self.assertIn("#2 left open, not a ledger: author human is not kube-agents-eval-bot", out)
        self.assertIn("closed 2 open ledger(s) in gke-agentic/kube-agents-evals-2-infra at lease time", out)

    def test_the_listing_asks_for_the_streams_labels(self):
        fake = FakeGitLab(issues=[issue(1)])
        self._reset(fake, audit_id="compliance-audit")
        listing = [p for m, p, _ in fake.calls if m == "GET" and "/issues" in p][0]
        self.assertIn("state=opened", listing)
        self.assertIn("labels=agent%3Aaudit%2Caudit%3Acompliance-audit", listing)

    def test_dry_run_writes_nothing(self):
        fake = FakeGitLab(issues=[issue(1)])
        unclosed, out, _ = self._reset(fake, dry_run=True)
        self.assertEqual(unclosed, 0)
        # The login read and the listing, nothing written.
        self.assertEqual([m for m, _, _ in fake.calls], ["GET", "GET"])
        self.assertIn("would close 1", out)

    def test_a_close_that_fails_is_counted_and_the_rest_still_close(self):
        fake = FakeGitLab(issues=[issue(1), issue(2)], fail={("PUT", "/issues/1")})
        unclosed, out, err = self._reset(fake)
        self.assertEqual(unclosed, 1)
        self.assertIn("#1 did not close", err)
        self.assertIn("closed 1 open ledger(s)", out)

    def test_the_wrong_project_is_refused_before_any_call(self):
        fake = FakeGitLab()
        with self.assertRaises(gitlab.ResetError):
            self._reset_other(fake)
        self.assertEqual(fake.calls, [])

    def _reset_other(self, fake):
        with mock.patch.object(gitlab, "api", fake), redirect_stdout(io.StringIO()):
            gitlab.reset_ledgers("gke-agentic/kube-agents-evals-9-infra", PROJECT, "b", "tok", None, False)


class MergeRequestResetTest(unittest.TestCase):
    def _reset(self, fake, dry_run=False):
        record = gitlab.empty_record()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gitlab, "api", fake), mock.patch.object(gitlab, "pause", lambda s: None), redirect_stdout(out), redirect_stderr(err):
            gitlab.reset_merge_requests(PATH, PROJECT, "b", "lease", "tok", dry_run, record)
        return record, out.getvalue(), err.getvalue(), fake

    def test_the_record_shape_is_the_github_resets(self):
        github = pulls.new_record(PATH, PROJECT, "b", "lease", False)
        self.assertEqual(set(gitlab.empty_record()), set(github) - {"schema_version", "forge", "repo", "project", "build", "scope", "dry_run", "started_at", "error"})

    def test_the_bots_merge_requests_close_and_every_branch_but_the_default_goes(self):
        fake = FakeGitLab(
            mrs=[mr(1), mr(2, labels=["audit:remediation"]), mr(3, author="human", source="human/work")],
            branches=["main", "platform-agent/fix", "human/work", "stray"],
        )
        record, out, _, fake = self._reset(fake)
        self.assertEqual(record["forge"], "gitlab")
        self.assertEqual(record["open_before"], 2)
        self.assertEqual(record["kept_open"], [3])
        self.assertEqual(record["labelled"], [2])
        self.assertEqual(record["closed"], [1, 2])
        self.assertEqual(record["kept_branches"], ["human/work"])
        self.assertEqual(sorted(record["deleted"]), ["platform-agent/fix", "stray"])
        self.assertTrue(record["clean"])
        self.assertEqual(record["open_after"], 0)
        self.assertEqual(record["branches_after"], 0)
        self.assertRegex(record["finished_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        writes = [(m, p.rsplit("/", 1)[1], b) for m, p, b in fake.calls if m != "GET"]
        # The audit's label goes on before its close, never after.
        self.assertEqual(writes[1], ("PUT", "2", {"add_labels": "audit:stale-closed"}))
        self.assertEqual(writes[2], ("PUT", "2", {"state_event": "close"}))
        self.assertIn("!3 left open, not the agent's (human/work)", out)

    def test_the_bot_is_whoever_the_token_belongs_to(self):
        # A renamed or replaced bot account is followed, as the GitHub sweep
        # follows the App's slug, rather than matched against a literal.
        fake = FakeGitLab(mrs=[mr(1, author="eval-bot-2"), mr(2, author=BOT)], branches=["main"], login="eval-bot-2")
        record, _, _, fake = self._reset(fake)
        self.assertEqual(record["closed"], [1])
        self.assertEqual(record["kept_open"], [2])
        self.assertIn(("GET", "/user", None), fake.calls)

    def test_a_refusal_still_records_the_forge(self):
        record = gitlab.empty_record()
        with self.assertRaises(gitlab.ResetError):
            gitlab.reset_merge_requests("gke-agentic/other-infra", PROJECT, "b", "s", "tok", False, record)
        self.assertEqual(record["forge"], "gitlab")

    def test_a_fork_source_is_not_the_agents_and_keeps_no_branch_here(self):
        fake = FakeGitLab(mrs=[mr(1, source_project=999, source="main")], branches=["main"])
        record, _, _, _ = self._reset(fake)
        self.assertEqual(record["kept_open"], [1])
        self.assertEqual(record["closed"], [])
        self.assertTrue(record["clean"])

    def test_a_refused_close_keeps_its_branch_and_is_not_clean(self):
        fake = FakeGitLab(mrs=[mr(1, source="keep-me")], branches=["main", "keep-me"], fail={("PUT", "/merge_requests/1")})
        record, _, err, _ = self._reset(fake)
        self.assertEqual(record["unclosed"], [1])
        self.assertEqual(record["kept_branches"], ["keep-me"])
        self.assertFalse(record["clean"])
        self.assertIn("!1 did not close", err)

    def test_a_branch_already_gone_is_not_a_fault(self):
        fake = FakeGitLab(branches=["main", "ghost"])
        # The listing names it; the delete finds it gone (404) and moves on.
        original = fake.__call__

        def racing(method, path, token, body=None, host=gitlab.DEFAULT_HOST):
            if method == "DELETE":
                fake.branches = ["main"]
            return original(method, path, token, body, host)

        record, _, _, _ = self._reset(racing)
        self.assertEqual(record["deleted"], ["ghost"])
        self.assertTrue(record["clean"])

    def test_dry_run_lists_and_writes_nothing(self):
        fake = FakeGitLab(mrs=[mr(1)], branches=["main", "platform-agent/fix"])
        record, out, _, fake = self._reset(fake, dry_run=True)
        self.assertEqual([m for m, _, _ in fake.calls if m != "GET"], [])
        self.assertFalse(record["clean"])
        self.assertIn("would close 1 merge request(s) and delete 1 branch(es)", out)


class RetryTest(unittest.TestCase):
    """Transient answers are tried again as the GitHub pulls reset does; a
    request's own fault is not."""

    def _call(self, answers):
        pauses = []
        api = mock.Mock(side_effect=answers)
        with mock.patch.object(gitlab, "api", api), mock.patch.object(gitlab, "pause", pauses.append), redirect_stderr(io.StringIO()):
            try:
                result = gitlab.call("GET", "/x", "tok")
            except Exception as exc:  # noqa: BLE001 - the test reads what escaped
                result = exc
        return result, api.call_count, pauses

    def _http(self, code, headers=None):
        return urllib.error.HTTPError("/x", code, "x", headers or {}, io.BytesIO(b""))

    def test_a_503_then_an_answer_costs_one_pause(self):
        result, calls, pauses = self._call([self._http(503), {"ok": True}])
        self.assertEqual((result, calls, pauses), ({"ok": True}, 2, [2]))

    def test_a_429_waits_what_retry_after_asks_capped(self):
        _, _, pauses = self._call([self._http(429, {"Retry-After": "5"}), {}])
        self.assertEqual(pauses, [5.0])
        _, _, pauses = self._call([self._http(429, {"Retry-After": "9999"}), {}])
        self.assertEqual(pauses, [float(gitlab.RETRY_AFTER_MAX_SECONDS)])

    def test_a_second_429_ends_the_run_as_rate_limited(self):
        result, calls, pauses = self._call([self._http(429, {"Retry-After": "3"}), self._http(429, {"Retry-After": "3"})])
        self.assertIsInstance(result, gitlab.RateLimited)
        self.assertEqual((calls, pauses), (2, [3.0]))
        # Whatever sits between the two, the last attempt included.
        for answers in (
            [self._http(503), self._http(429, {"Retry-After": "1"}), self._http(429, {"Retry-After": "1"})],
            [self._http(429, {"Retry-After": "1"}), self._http(503), self._http(429, {"Retry-After": "1"})],
            # A first 429 with no attempt left to wait for is the limit too.
            [self._http(503), self._http(503), self._http(429, {"Retry-After": "1"})],
        ):
            with self.subTest(answers=[getattr(a, "code", a) for a in answers]):
                result, _, _ = self._call(answers)
                self.assertIsInstance(result, gitlab.RateLimited)

    def test_a_write_loop_lets_rate_limited_through(self):
        fake = FakeGitLab(mrs=[mr(1), mr(2)], branches=["main"])
        original = fake.__call__

        def limited(method, path, token, body=None, host=gitlab.DEFAULT_HOST):
            if method == "PUT":
                raise urllib.error.HTTPError(path, 429, "x", {"Retry-After": "1"}, io.BytesIO(b""))
            return original(method, path, token, body, host)

        with mock.patch.object(gitlab, "api", limited), mock.patch.object(gitlab, "pause", lambda s: None), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(gitlab.RateLimited):
                gitlab.reset_merge_requests(PATH, PROJECT, "b", "s", "tok", False, gitlab.empty_record())

    def test_a_dropped_connection_is_retried_twice_then_raised(self):
        result, calls, pauses = self._call([OSError("reset"), OSError("reset"), OSError("reset")])
        self.assertIsInstance(result, OSError)
        self.assertEqual((calls, pauses), (3, [2, 8]))

    def test_a_404_is_the_requests_fault_and_not_retried(self):
        result, calls, pauses = self._call([self._http(404)])
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual((calls, pauses), (1, []))

    def test_the_record_timestamp_format_is_the_github_resets(self):
        self.assertEqual(gitlab.ISO_UTC_FORMAT, pulls.ISO_UTC_FORMAT)


class TokenExpiryTest(unittest.TestCase):
    def _entry(self, expires, today=datetime.date(2026, 10, 6), active=True, revoked=False):
        fake = mock.Mock(return_value={"name": "pool-agent", "scopes": ["write_repository", "api"], "expires_at": expires, "active": active, "revoked": revoked})
        with mock.patch.object(gitlab, "api", fake):
            return gitlab.token_expiry("tok", today=today)

    def test_a_year_out_is_quiet(self):
        entry = self._entry("2027-10-05")
        self.assertEqual(entry["days_left"], 364)
        self.assertFalse(entry["warn"])
        self.assertEqual(entry["scopes"], ["api", "write_repository"])
        self.assertIsNone(gitlab.expiry_message(entry, "kube-agents-prow/gitlab-agent-token"))

    def test_thirty_days_out_warns_and_seven_is_urgent(self):
        warn = self._entry("2026-11-05")
        self.assertTrue(warn["warn"])
        self.assertFalse(warn["urgent"])
        self.assertIn("in 30 day(s): rotate it soon, with overlap", gitlab.expiry_message(warn, "s"))
        urgent = self._entry("2026-10-10")
        self.assertTrue(urgent["urgent"])
        self.assertIn("rotate it urgently", gitlab.expiry_message(urgent, "s"))

    def test_a_revoked_token_is_named(self):
        entry = self._entry("2027-10-05", revoked=True)
        self.assertIn("is not active", gitlab.expiry_message(entry, "s"))

    def test_a_token_without_an_expiry_is_a_fault(self):
        with mock.patch.object(gitlab, "api", mock.Mock(return_value={"name": "x"})):
            with self.assertRaises(gitlab.ResetError):
                gitlab.token_expiry("tok")


class ResetDispatchTest(unittest.TestCase):
    """`--forge gitlab` on the two reset helpers reaches the GitLab module and
    nothing of the GitHub path; the default is GitHub, unchanged."""

    def _main(self, module, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(out), redirect_stderr(err):
            code = module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_the_ledger_reset_dispatches_to_gitlab(self):
        fake = FakeGitLab(issues=[issue(1)])
        with mock.patch.object(gitlab, "api", fake), mock.patch.object(ledgers, "api", mock.Mock(side_effect=AssertionError("GitHub was called"))):
            code, out, _ = self._main(ledgers, ["--repo", PATH, "--project", PROJECT, "--build", "b", "--forge", "gitlab"], {"LEDGER_RESET_TOKEN": "tok"})
        self.assertEqual(code, 0)
        self.assertIn("closed 1 open ledger(s)", out)

    def test_the_pull_reset_dispatches_to_gitlab_and_records_the_forge(self):
        fake = FakeGitLab(mrs=[mr(1)], branches=["main", "platform-agent/fix"])
        with tempfile.TemporaryDirectory() as tmp:
            record = pathlib.Path(tmp) / "r.json"
            with mock.patch.object(gitlab, "api", fake), mock.patch.object(gitlab, "pause", lambda s: None), mock.patch.object(ledgers, "api", mock.Mock(side_effect=AssertionError("GitHub was called"))):
                code, out, _ = self._main(pulls, ["--repo", PATH, "--project", PROJECT, "--build", "b", "--forge", "gitlab", "--record", str(record)], {"AGENT_PULLS_RESET_TOKEN": "tok"})
            document = json.loads(record.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(document["forge"], "gitlab")
        self.assertTrue(document["clean"])
        self.assertEqual(document["closed"], [1])

    def test_the_github_default_records_github(self):
        self.assertEqual(pulls.new_record(PATH, PROJECT, "b", "s", False)["forge"], "github")

    def test_a_refusal_is_an_error_line_when_the_ledger_reset_runs_as_a_script(self):
        # As a script the module is __main__, and the GitLab module's import of
        # it by name is a second copy with a ResetError of its own; the guard's
        # refusal has to come out as ERROR and exit 2 all the same. No network:
        # the project guard refuses before any call.
        proc = subprocess.run(
            [sys.executable, str(HACK / "ci_reset_audit_ledgers.py"), "--forge", "gitlab", "--repo", "gke-agentic/other-infra", "--project", PROJECT, "--build", "b"],
            capture_output=True, text=True, env={**os.environ, "LEDGER_RESET_TOKEN": "tok"}, check=False,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("ERROR: gke-agentic/other-infra is not the GitLab project of the leased project", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_rate_limit_is_an_error_line_in_both_resets(self):
        limited = mock.Mock(side_effect=urllib.error.HTTPError("/x", 429, "x", {"Retry-After": "1"}, io.BytesIO(b"")))
        with mock.patch.object(gitlab, "api", limited), mock.patch.object(gitlab, "pause", lambda s: None):
            code, _, err = self._main(ledgers, ["--repo", PATH, "--project", PROJECT, "--build", "b", "--forge", "gitlab"], {"LEDGER_RESET_TOKEN": "tok"})
        self.assertEqual(code, 1)
        self.assertIn("ERROR: GitLab answered HTTP 429 on", err)
        with tempfile.TemporaryDirectory() as tmp:
            record = pathlib.Path(tmp) / "r.json"
            with mock.patch.object(gitlab, "api", limited), mock.patch.object(gitlab, "pause", lambda s: None):
                code, _, err = self._main(pulls, ["--repo", PATH, "--project", PROJECT, "--build", "b", "--forge", "gitlab", "--record", str(record)], {"AGENT_PULLS_RESET_TOKEN": "tok"})
            document = json.loads(record.read_text())
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429 on", document["error"])
        self.assertFalse(document["clean"])

    def test_a_read_cut_short_three_times_is_an_error_line_in_the_ledger_reset(self):
        import http.client

        cut = mock.Mock(side_effect=http.client.IncompleteRead(b""))
        with mock.patch.object(gitlab, "api", cut), mock.patch.object(gitlab, "pause", lambda s: None):
            code, _, err = self._main(ledgers, ["--repo", PATH, "--project", PROJECT, "--build", "b", "--forge", "gitlab"], {"LEDGER_RESET_TOKEN": "tok"})
        self.assertEqual(code, 1)
        self.assertIn("ERROR: could not reach gitlab.com (IncompleteRead", err)

    def test_an_unreachable_gitlab_names_gitlab(self):
        boom = mock.Mock(side_effect=OSError("connection refused"))
        with mock.patch.object(gitlab, "api", boom), mock.patch.object(gitlab, "pause", lambda s: None):
            code, _, err = self._main(ledgers, ["--repo", PATH, "--project", PROJECT, "--build", "b", "--forge", "gitlab"], {"LEDGER_RESET_TOKEN": "tok"})
        self.assertEqual(code, 1)
        self.assertIn("could not reach gitlab.com", err)


def _fake_expiry(token, items):
    """A gitlab_token_expiry stand-in that fills the caller's entries list as the real one does."""

    def expiry(runner, today=None, entries=None):
        if entries is not None:
            entries.extend(items)
        return token, list(items)

    return expiry


def _runner(stdout=b"glpat-fake", code=0, stderr=b""):
    """A subprocess.run stand-in that records argv and answers gcloud."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


class SweepGitLabPassTest(unittest.TestCase):
    def test_the_gitlab_mapping_is_read_from_the_deploy_script(self):
        mapping = sweeper.pool_repos(CI_DEPLOY, sweeper.GITLAB_MAPPING_FUNCTION)
        self.assertEqual(mapping[PROJECT], PATH)
        self.assertEqual(len(mapping), len(sweeper.pool_repos(CI_DEPLOY)))

    def test_the_token_is_read_through_gcloud_and_never_an_argument(self):
        run = _runner(stdout=b"glpat-fake\n")
        token = sweeper.gitlab_secret(sweeper.GITLAB_AGENT_SECRET, run)
        self.assertEqual(token, "glpat-fake")
        self.assertEqual(run.calls[0][:5], ["gcloud", "secrets", "versions", "access", "latest"])
        self.assertIn("--project=kube-agents-prow", run.calls[0])
        self.assertNotIn("glpat-fake", " ".join(run.calls[0]))

    def test_a_secret_that_is_not_one_line_is_refused_without_echoing_it(self):
        for value in (b"glpat-abc\nglpat-def\n", b"glpat-abc def", b"glpat-abc\x01", b"\xff\xfeg\x00l\x00"):
            with self.subTest(value=value):
                with self.assertRaises(sweeper.SweepError) as caught:
                    sweeper.gitlab_secret(sweeper.GITLAB_AGENT_SECRET, _runner(stdout=value))
                self.assertIn("not one token on one line", str(caught.exception))
                self.assertNotIn("glpat", str(caught.exception))

    def test_a_dry_run_counts_what_it_would_close(self):
        fake = FakeGitLab(mrs=[mr(1), mr(2)], branches=["main", "platform-agent/fix"])
        with mock.patch.object(gitlab, "api", fake), redirect_stdout(io.StringIO()):
            self.assertEqual(sweeper.sweep_gitlab_project(PROJECT, PATH, "tok", dry_run=True), 2)
        self.assertEqual([m for m, _, _ in fake.calls if m != "GET"], [])

    def test_a_ledger_token_read_that_drops_is_a_due_entry_too(self):
        calls = iter([
            {"name": "pool-agent", "scopes": ["api"], "expires_at": "2027-10-05", "active": True, "revoked": False},
            OSError("connection reset"), OSError("connection reset"), OSError("connection reset"),
        ])
        def api(method, path, token, body=None, host=gitlab.DEFAULT_HOST):
            answer = next(calls)
            if isinstance(answer, Exception):
                raise answer
            return answer
        entries = []
        with mock.patch.object(gitlab, "api", api), mock.patch.object(gitlab, "pause", lambda s: None), redirect_stderr(io.StringIO()):
            sweeper.gitlab_token_expiry(_runner(stdout=b"glpat-x"), today=datetime.date(2026, 10, 6), entries=entries)
        self.assertEqual([e["name"] for e in entries], ["pool-agent", "gitlab-ledger-token"])
        self.assertIn("could not be checked", entries[1]["error"])

    def test_every_failing_exit_names_a_due_token(self):
        due = {"name": "pool-agent", "secret": "kube-agents-prow/gitlab-agent-token", "expires_at": "2026-11-01", "days_left": 26, "active": True, "warn": True, "urgent": False, "scopes": ["api"]}
        for fault in (urllib.error.HTTPError("/x", 404, "Not Found", {}, io.BytesIO(b"")), OSError("gone")):
            with self.subTest(fault=type(fault).__name__), tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                sweeper, "sweep_gitlab_project", mock.Mock(side_effect=fault)
            ), mock.patch.object(sweeper, "gitlab_token_expiry", _fake_expiry("glpat-fake", [due])), mock.patch.object(
                sweeper.signal, "signal"
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                report = pathlib.Path(tmp) / "r.json"
                code = sweeper.main(["--forge", "gitlab", "--project", PROJECT, "--ci-deploy-script", str(CI_DEPLOY), "--report", str(report)])
                document = json.loads(report.read_text())
                self.assertEqual(code, 1)
                self.assertIn("pool-agent", document["error"])

    def test_a_read_gcloud_refuses_names_the_grant(self):
        with self.assertRaises(sweeper.SweepError) as caught:
            sweeper.gitlab_secret(sweeper.GITLAB_AGENT_SECRET, _runner(code=1, stderr=b"PERMISSION_DENIED"))
        self.assertIn("secretAccessor", str(caught.exception))

    def test_a_due_token_is_reported_not_failed_and_a_dead_one_fails_the_run(self):
        # A month of red sweeps would read as failed projects to CI health;
        # the due token is a warning line and a report entry (kube-agents#2571
        # carries it), and only a token that is dead or unchecked fails the run.
        entry = {"name": "pool-agent", "secret": "kube-agents-prow/gitlab-agent-token", "expires_at": "2026-11-01", "days_left": 26, "active": True, "warn": True, "urgent": False, "scopes": ["api"]}
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(sweeper._expiry_verdict({"gitlab_tokens": [entry]}), (0, None))
            self.assertEqual(sweeper._expiry_verdict({"gitlab_tokens": [dict(entry, days_left=3, urgent=True)]}), (0, None))
            code, error = sweeper._expiry_verdict({"gitlab_tokens": [entry, dict(entry, name="pool-ledger", active=False)]})
        self.assertEqual(code, 1)
        self.assertIn("pool-ledger", error)
        self.assertNotIn("pool-agent", error, "the due one is not in the failure")
        self.assertIn("is not active", err.getvalue())
        with redirect_stderr(io.StringIO()):
            code, error = sweeper._expiry_verdict({"gitlab_tokens": [dict(entry, active=False, error="the token in kube-agents-prow/gitlab-ledger-token no longer authenticates (HTTP 401)")]})
        self.assertEqual((code, "no longer authenticates" in error), (1, True))

    def test_the_pool_walk_under_gitlab_runs_through_main_with_the_shared_token(self):
        """`--forge gitlab --pool` as the periodic runs it: main reads the token
        pair, and _run hands the forge and the token to the pool walk."""
        seen = []

        def fake_walk(server, owner, hold_state, pool_size, visit, heartbeat=False, release_failures=None):
            visit(PROJECT)
            return [PROJECT], {}

        def fake_sweep(project, path, token, dry_run=False):
            seen.append((project, path, token))
            return 2

        report = pathlib.Path(tempfile.mkdtemp()) / "pull-sweep-gitlab.json"
        with mock.patch.object(sweeper, "gitlab_token_expiry", lambda runner, entries=None: ("glpat-pool", entries)), mock.patch.object(sweeper.boskos_pool, "walk", fake_walk), mock.patch.object(
            sweeper.boskos_pool, "reset_stranded", lambda *a: None
        ), mock.patch.object(sweeper, "sweep_gitlab_project", fake_sweep), mock.patch.object(sweeper, "sweep_repo", mock.Mock(side_effect=AssertionError("GitHub sweep was called"))), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = sweeper.main(["--forge", "gitlab", "--pool", "--boskos-server", "http://boskos", "--boskos-owner", "owner", "--report", str(report)])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [(PROJECT, PATH, "glpat-pool")])
        document = json.loads(report.read_text())
        self.assertEqual((document["forge"], document["projects"], document["closed"]), ("gitlab", 1, 2))

    def test_a_hand_run_on_one_project_uses_the_shared_token_and_the_gitlab_closer(self):
        seen = {}

        def fake_sweep(project, path, token, dry_run=False):
            seen.update(project=project, path=path, token=token)
            return 2

        quiet = {"name": "a", "secret": "s", "expires_at": "2027-10-05", "days_left": 364, "active": True, "warn": False, "urgent": False, "scopes": []}
        # signal.signal is mocked as the sweep's own suite does: a real
        # handler installed here would outlive the test and change what the
        # Boskos hold does in suites that run after this one.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sweeper, "sweep_gitlab_project", fake_sweep), mock.patch.object(
            sweeper, "gitlab_token_expiry", _fake_expiry("glpat-fake", [quiet, quiet])
        ), mock.patch.object(sweeper, "sweep_repo", mock.Mock(side_effect=AssertionError("GitHub sweep was called"))), mock.patch.object(
            sweeper.signal, "signal"
        ), redirect_stdout(io.StringIO()):
            report = pathlib.Path(tmp) / "r.json"
            code = sweeper.main(["--forge", "gitlab", "--project", PROJECT, "--ci-deploy-script", str(CI_DEPLOY), "--report", str(report)])
            document = json.loads(report.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"project": PROJECT, "path": PATH, "token": "glpat-fake"})
        self.assertEqual(document["forge"], "gitlab")
        self.assertEqual(document["closed"], 2)
        self.assertEqual(len(document["gitlab_tokens"]), 2)

    def test_the_gitlab_report_has_its_own_file(self):
        with mock.patch.dict(os.environ, {"ARTIFACTS": "/a"}):
            self.assertEqual(sweeper.default_report_path("gitlab"), "/a/pull-sweep-gitlab.json")
            self.assertEqual(sweeper.default_report_path(), "/a/pull-sweep.json")

    def test_a_rate_limited_project_ends_the_run_like_the_github_pass(self):
        limited = mock.Mock(side_effect=urllib.error.HTTPError("/x", 429, "x", {"Retry-After": "1"}, io.BytesIO(b"")))
        with mock.patch.object(gitlab, "api", limited), mock.patch.object(gitlab, "pause", lambda s: None), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(sweeper.RateLimited):
                sweeper.sweep_gitlab_project(PROJECT, PATH, "tok")

    def test_a_termination_names_a_due_token_too(self):
        due = {"name": "pool-agent", "secret": "kube-agents-prow/gitlab-agent-token", "expires_at": "2026-11-01", "days_left": 26, "active": True, "warn": True, "urgent": False, "scopes": ["api"]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            sweeper, "sweep_gitlab_project", mock.Mock(side_effect=sweeper.Terminated("SIGTERM"))
        ), mock.patch.object(sweeper, "gitlab_token_expiry", _fake_expiry("glpat-fake", [due])), mock.patch.object(
            sweeper.signal, "signal"
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            report = pathlib.Path(tmp) / "r.json"
            code = sweeper.main(["--forge", "gitlab", "--project", PROJECT, "--ci-deploy-script", str(CI_DEPLOY), "--report", str(report)])
            document = json.loads(report.read_text())
        self.assertEqual(code, sweeper.TERMINATED_EXIT_CODE)
        self.assertIn("terminated", document["error"])
        self.assertIn("pool-agent", document["error"])

    def test_a_termination_mid_project_keeps_the_close_count(self):
        def closes_one_then_dies(path, project, build, scope, token, dry_run, record):
            record["closed"].append(7)
            raise sweeper.Terminated("SIGTERM")

        with mock.patch.object(gitlab, "reset_merge_requests", closes_one_then_dies):
            with self.assertRaises(sweeper.Terminated) as caught:
                sweeper.sweep_gitlab_project(PROJECT, PATH, "tok")
        self.assertEqual(caught.exception.closed, 1)

    def test_a_rate_limited_token_lookup_ends_the_run_naming_the_secret(self):
        limited = mock.Mock(side_effect=urllib.error.HTTPError("/self", 429, "x", {"Retry-After": "1"}, io.BytesIO(b"")))
        with mock.patch.object(gitlab, "api", limited), mock.patch.object(gitlab, "pause", lambda s: None):
            with self.assertRaises(sweeper.RateLimited) as caught:
                sweeper.gitlab_token_expiry(_runner(stdout=b"glpat-x"))
        self.assertIn("kube-agents-prow/gitlab-agent-token", str(caught.exception))
        # Through main: an ERROR line and exit "failed", not a traceback.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            sweeper, "gitlab_token_expiry", mock.Mock(side_effect=sweeper.RateLimited("looking up the token in s: limited"))
        ), mock.patch.object(sweeper.signal, "signal"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            report = pathlib.Path(tmp) / "r.json"
            code = sweeper.main(["--forge", "gitlab", "--project", PROJECT, "--ci-deploy-script", str(CI_DEPLOY), "--report", str(report)])
            document = json.loads(report.read_text())
        self.assertEqual(code, 1)
        self.assertIn("ERROR: looking up the token in s: limited", err.getvalue())
        self.assertEqual((document["exit"], document["ended_early"]), ("failed", "looking up the token in s: limited"))

    def test_a_token_that_is_due_is_named_even_when_a_project_failed(self):
        due = {"name": "pool-agent", "secret": "kube-agents-prow/gitlab-agent-token", "expires_at": "2026-11-01", "days_left": 26, "active": True, "warn": True, "urgent": False, "scopes": ["api"]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            sweeper, "sweep_gitlab_project", mock.Mock(side_effect=sweeper.SweepError("left 1 branch(es): stray"))
        ), mock.patch.object(sweeper, "gitlab_token_expiry", _fake_expiry("glpat-fake", [due])), mock.patch.object(
            sweeper.signal, "signal"
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            report = pathlib.Path(tmp) / "r.json"
            code = sweeper.main(["--forge", "gitlab", "--project", PROJECT, "--ci-deploy-script", str(CI_DEPLOY), "--report", str(report)])
            document = json.loads(report.read_text())
        self.assertEqual(code, 1)
        self.assertIn("left 1 branch(es): stray", document["error"])
        self.assertIn("pool-agent", document["error"])
        self.assertIn("rotate it soon", document["error"])

    def test_a_dead_agent_token_is_named_with_its_secret(self):
        dead = mock.Mock(side_effect=urllib.error.HTTPError("/self", 401, "Unauthorized", {}, io.BytesIO(b"")))
        with mock.patch.object(gitlab, "api", dead):
            with self.assertRaises(sweeper.SweepError) as caught:
                sweeper.gitlab_token_expiry(_runner(stdout=b"glpat-x"))
        self.assertIn("kube-agents-prow/gitlab-agent-token no longer authenticates (HTTP 401)", str(caught.exception))
        odd = mock.Mock(return_value={"name": "x"})
        with mock.patch.object(gitlab, "api", odd):
            with self.assertRaises(sweeper.SweepError) as caught:
                sweeper.gitlab_token_expiry(_runner(stdout=b"glpat-x"))
        self.assertIn("could not be checked", str(caught.exception))

    def test_a_dead_ledger_token_is_a_due_entry_and_the_sweep_still_runs(self):
        # The sweep needs the agent token only; a ledger token that no longer
        # authenticates is recorded as a dead entry and fails the run at the
        # end, after the walk (_expiry_verdict).
        answers = iter([
            {"name": "pool-agent", "scopes": ["api", "write_repository"], "expires_at": "2027-10-05", "active": True, "revoked": False},
            urllib.error.HTTPError("/self", 401, "Unauthorized", {}, io.BytesIO(b"")),
        ])
        def api(method, path, token, body=None, host=gitlab.DEFAULT_HOST):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        entries = []
        with mock.patch.object(gitlab, "api", api):
            token, _ = sweeper.gitlab_token_expiry(_runner(stdout=b"glpat-x"), today=datetime.date(2026, 10, 6), entries=entries)
        self.assertEqual(token, "glpat-x")
        self.assertEqual([e["name"] for e in entries], ["pool-agent", "gitlab-ledger-token"])
        self.assertFalse(entries[1]["active"])
        self.assertIn("gitlab-ledger-token no longer authenticates", entries[1]["error"])
        # Through main, with a clean project: the walk ran, and the run fails naming the ledger token.
        swept = mock.Mock(return_value=0)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sweeper, "sweep_gitlab_project", swept), mock.patch.object(
            sweeper, "gitlab_token_expiry", _fake_expiry("glpat-x", list(entries))
        ), mock.patch.object(sweeper.signal, "signal"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            report = pathlib.Path(tmp) / "r.json"
            code = sweeper.main(["--forge", "gitlab", "--project", PROJECT, "--ci-deploy-script", str(CI_DEPLOY), "--report", str(report)])
            document = json.loads(report.read_text())
        self.assertEqual(code, 1)
        self.assertTrue(swept.called)
        self.assertIn("gitlab-ledger-token no longer authenticates", document["error"])
        self.assertEqual(len(document["gitlab_tokens"]), 2)

    def test_a_hand_run_listing_fault_is_recorded_as_the_projects_failure(self):
        fault = urllib.error.HTTPError("/projects/x", 404, "Not Found", {}, io.BytesIO(b""))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            sweeper, "sweep_gitlab_project", mock.Mock(side_effect=fault)
        ), mock.patch.object(sweeper, "gitlab_token_expiry", _fake_expiry("glpat-fake", [])), mock.patch.object(
            sweeper.signal, "signal"
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            report = pathlib.Path(tmp) / "r.json"
            code = sweeper.main(["--forge", "gitlab", "--project", PROJECT, "--ci-deploy-script", str(CI_DEPLOY), "--report", str(report)])
            document = json.loads(report.read_text())
        self.assertEqual(code, 1)
        self.assertEqual((document["projects"], document["failed"]), (1, 1))
        self.assertIn("404", document["outcomes"][PROJECT]["error"])

    def test_the_pool_walk_under_gitlab_sweeps_with_the_shared_token(self):
        # The mode the periodic runs: Boskos hands out projects, each is swept
        # by the GitLab closer with the pool's token, never by the GitHub one.
        seen = []

        def fake_walk(server, owner, hold_state, pool_size, visit, heartbeat=False, release_failures=None):
            for name in (PROJECT, "kube-agents-evals-3"):
                visit(name)
            return [PROJECT, "kube-agents-evals-3"], {}

        def fake_sweep(project, path, token, dry_run=False):
            seen.append((project, path, token))
            return 1

        run = {}
        with mock.patch.object(sweeper.boskos_pool, "walk", fake_walk), mock.patch.object(sweeper.boskos_pool, "reset_stranded", lambda *a: None), mock.patch.object(
            sweeper, "sweep_gitlab_project", fake_sweep
        ), mock.patch.object(sweeper, "sweep_repo", mock.Mock(side_effect=AssertionError("GitHub sweep was called"))), redirect_stdout(io.StringIO()):
            closed, failures, unmapped = sweeper.sweep_pool("http://boskos", "owner", "app", sweeper.pool_repos(CI_DEPLOY, sweeper.GITLAB_MAPPING_FUNCTION), report=run, forge="gitlab", gitlab_token="glpat-pool")
        self.assertEqual(seen, [(PROJECT, PATH, "glpat-pool"), ("kube-agents-evals-3", "gke-agentic/kube-agents-evals-3-infra", "glpat-pool")])
        self.assertEqual((closed, failures, unmapped), ({PROJECT: 1, "kube-agents-evals-3": 1}, {}, []))

    def test_a_project_the_module_refuses_is_the_projects_failure_not_the_runs(self):
        # A lookup without a default branch (an empty project) is a ResetError
        # inside the module; the sweep reports it like any other project fault
        # and the walk goes on, rather than a traceback that abandons the pool.
        empty = mock.Mock(return_value={"id": 1, "default_branch": None, "username": BOT})
        with mock.patch.object(gitlab, "api", empty), redirect_stdout(io.StringIO()):
            with self.assertRaises(sweeper.SweepError) as caught:
                sweeper.sweep_gitlab_project(PROJECT, PATH, "tok")
        self.assertIn("without an id and a default branch", str(caught.exception))

    def test_a_clean_project_sweep_returns_the_count_and_a_dirty_one_raises(self):
        fake = FakeGitLab(mrs=[mr(1)], branches=["main", "platform-agent/fix"])
        with mock.patch.object(gitlab, "api", fake), mock.patch.object(gitlab, "pause", lambda s: None), redirect_stdout(io.StringIO()):
            self.assertEqual(sweeper.sweep_gitlab_project(PROJECT, PATH, "tok"), 1)
        dirty = FakeGitLab(mrs=[mr(1)], branches=["main", "platform-agent/fix"], fail={("PUT", "/merge_requests/1")})
        with mock.patch.object(gitlab, "api", dirty), mock.patch.object(gitlab, "pause", lambda s: None), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(sweeper.SweepError) as caught:
                sweeper.sweep_gitlab_project(PROJECT, PATH, "tok")
        self.assertIn("left 1 merge request(s) open: !1", str(caught.exception))


# --- the eval script ------------------------------------------------------------


def lifted(name):
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.DOTALL | re.MULTILINE)
    assert match, f"{name}() not found in {SCRIPT}"
    return match.group(0)


def lifted_line(pattern):
    match = re.search(pattern, SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, pattern
    return match.group(0)


def literal(path, name):
    match = re.search(rf'^{name}="([^"]*)"$', path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} not found in {path}"
    return match.group(1)


def run_bash(body, env=None, bin_dir=None):
    environment = {**os.environ, **(env or {})}
    if bin_dir:
        environment["PATH"] = f"{bin_dir}:{environment.get('PATH', '')}"
    return subprocess.run(["bash", "-c", "set -uo pipefail\n" + body], capture_output=True, text=True, check=False, env=environment)


STUB_WHOAMI = textwrap.dedent(
    """\
    import json, os, sys
    token = os.environ.get("GITLAB_PROBE_TOKEN", "")
    with open(os.environ["WHOAMI_LOG"], "a") as log:
        log.write("WHOAMI argv=" + json.dumps(sys.argv[2:]) + " token=" + token + "\\n")
    if sys.argv[1:2] != ["whoami"]:
        sys.exit(2)
    if token in os.environ.get("WHOAMI_DEAD", "").split(","):
        print("the token no longer authenticates at gitlab.com (HTTP 401)", file=sys.stderr)
        sys.exit(1)
    print(os.environ.get("WHOAMI_LOGIN", "kube-agents-eval-bot"))
    """
)

STUB_HELPER = textwrap.dedent(
    """\
    import json, os, sys
    print("HELPER argv=" + json.dumps(sys.argv[1:]))
    print("HELPER token=" + os.environ.get("LEDGER_RESET_TOKEN", os.environ.get("AGENT_PULLS_RESET_TOKEN", "")))
    sys.exit(0)
    """
)


class EvalScriptGitLabTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        (self.dir / "ci_reset_audit_ledgers.py").write_text(STUB_HELPER)
        (self.dir / "ci_reset_agent_pulls.py").write_text(STUB_HELPER)
        (self.dir / "ci_gitlab_forge.py").write_text(STUB_WHOAMI)
        (self.dir / "ci-deploy.sh").write_text(CI_DEPLOY.read_text(encoding="utf-8"))

    def _run(self, body, dead=""):
        """run_bash with the whoami stub's knobs: its log, and the token values it refuses."""
        return run_bash(body, env={"WHOAMI_LOG": f"{self.dir}/whoami.log", "WHOAMI_DEAD": dead}, bin_dir=self.bin)

    def _gcloud(self, agent="glpat-agent", ledger="glpat-ledger", fail=""):
        stub = self.bin / "gcloud"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.dir}/gcloud.argv"\n'
            f'case "$*" in *"{fail}"*) [ -n "{fail}" ] && exit 1 ;; esac\n'
            f'case "$*" in *gitlab-agent-token*) printf "%s\\n" "{agent}" ;; *gitlab-ledger-token*) printf "%s\\n" "{ledger}" ;; esac\n'
        )
        stub.chmod(0o755)

    def _preamble(self):
        return "\n".join(
            [
                'EVAL_FORGE="gitlab"',
                # The assignment is lifted, not restated: the line that fills
                # the array is what the argv assertions below pin.
                lifted_line(r"^EVAL_FORGE_HELPER_ARGS=\(\)$"),
                lifted_line(r'^\[ "\$\{EVAL_FORGE\}" = "gitlab" \] && EVAL_FORGE_HELPER_ARGS=\(--forge gitlab\)$'),
                f'GITLAB_SECRETS_PROJECT="{literal(SCRIPT, "GITLAB_SECRETS_PROJECT")}"',
                f'GITLAB_AGENT_SM_SECRET="{literal(SCRIPT, "GITLAB_AGENT_SM_SECRET")}"',
                f'GITLAB_LEDGER_SM_SECRET="{literal(SCRIPT, "GITLAB_LEDGER_SM_SECRET")}"',
                f'GITLAB_BOT_LOGIN="{literal(SCRIPT, "GITLAB_BOT_LOGIN")}"',
                f'GITLAB_FORGE_HOST="{literal(SCRIPT, "GITLAB_FORGE_HOST")}"',
                f'SCRIPT_DIR="{self.dir}"; PROJECT_ID="{PROJECT}"; BUILD_ID=1; EVAL_LEDGER_APP_KEY_FILE=""; EVAL_LEDGER_APP_ID=1',
                f'ARTIFACT_DIR="{self.dir}/artifacts"; AGENT_PULLS_RESET_PERMISSIONS=\'{{}}\'',
                'EVAL_GITLAB_AGENT_TOKEN=""',
                lifted("read_gitlab_tokens"),
                lifted("eval_gitlab_project"),
                lifted("forge_write_token"),
                lifted("reset_audit_ledgers"),
                lifted("reset_agent_pulls"),
            ]
        )

    def test_the_secret_names_equal_the_deploys(self):
        for name in ("GITLAB_SECRETS_PROJECT", "GITLAB_AGENT_SM_SECRET", "GITLAB_FORGE_HOST"):
            with self.subTest(name=name):
                self.assertEqual(literal(SCRIPT, name), literal(CI_DEPLOY, name))
        # Pinned as literals too: a test that only compares the two files would
        # pass with both pointing somewhere wrong.
        self.assertEqual(literal(SCRIPT, "GITLAB_SECRETS_PROJECT"), "kube-agents-prow")
        self.assertEqual(literal(SCRIPT, "GITLAB_LEDGER_SM_SECRET"), "gitlab-ledger-token")
        self.assertEqual(literal(SCRIPT, "GITLAB_BOT_LOGIN"), BOT)

    def test_the_pair_is_read_once_and_exported_as_the_bench_reads_it(self):
        self._gcloud()
        body = self._preamble() + '\nexport BENCH_GITHUB_TOKEN=ghs_mounted GITHUB_TOKEN=ghp_ambient\nread_gitlab_tokens; echo "RC=$?"\necho "FORGE=${BENCH_FORGE} LEDGER=${BENCH_GITLAB_TOKEN} HOST=${BENCH_GITLAB_HOST} LOGIN=${BENCH_GITLAB_AGENT_LOGIN} AGENT=${EVAL_GITLAB_AGENT_TOKEN} GH=${BENCH_GITHUB_TOKEN:-unset}/${GITHUB_TOKEN:-unset}"\n'
        proc = self._run(body)
        self.assertIn("RC=0", proc.stdout)
        # A bench without the GitLab checks must find no GitHub token to fall back on.
        self.assertIn("GH=unset/unset", proc.stdout)
        self.assertIn("FORGE=gitlab LEDGER=glpat-ledger HOST=gitlab.com LOGIN=kube-agents-eval-bot AGENT=glpat-agent", proc.stdout)
        argv = (self.dir / "gcloud.argv").read_text()
        self.assertEqual(argv.count("secrets versions access latest"), 2)
        self.assertIn("--project=kube-agents-prow", argv)
        self.assertNotIn("glpat", argv)
        # Both tokens are proven at the forge before anything runs, each in the probe's environment, never on its argv.
        probes = (self.dir / "whoami.log").read_text().splitlines()
        self.assertEqual(probes, ['WHOAMI argv=["--host", "gitlab.com"] token=glpat-agent', 'WHOAMI argv=["--host", "gitlab.com"] token=glpat-ledger'])
        self.assertIn("the agent token in kube-agents-prow/gitlab-agent-token authenticates as kube-agents-eval-bot", proc.stdout)
        self.assertNotIn("WARNING", proc.stderr)

    def test_a_token_that_no_longer_authenticates_stops_the_run_at_preflight(self):
        self._gcloud()
        proc = self._run(self._preamble() + '\nread_gitlab_tokens; echo "RC=$?"\n', dead="glpat-ledger")
        self.assertIn("RC=1", proc.stdout)
        self.assertIn("the ledger token in kube-agents-prow/gitlab-ledger-token does not authenticate at gitlab.com", proc.stderr)
        self.assertIn("create a new one", proc.stderr)
        # An agent token owned by some other account is said, not refused: the resets follow the token, the bench the configured login.
        proc = self._run(self._preamble() + '\nread_gitlab_tokens; echo "RC=$?"\n' , dead="")
        self.assertIn("RC=0", proc.stdout)
        proc = run_bash(self._preamble() + '\nread_gitlab_tokens; echo "RC=$?"\n', env={"WHOAMI_LOG": f"{self.dir}/whoami.log", "WHOAMI_DEAD": "", "WHOAMI_LOGIN": "someone-else"}, bin_dir=self.bin)
        self.assertIn("RC=0", proc.stdout)
        self.assertIn("the agent token belongs to someone-else, not kube-agents-eval-bot", proc.stderr)

    def test_the_preflight_read_stops_the_run_and_precedes_the_lease_reset(self):
        """The function's return code is one half; the other is the line that
        turns it into a stopped run, under the gitlab branch of the preflight."""
        src = SCRIPT.read_text(encoding="utf-8")
        branch = src.index('if [ "${EVAL_FORGE}" = "gitlab" ]; then\n  read_gitlab_tokens || exit 1')
        self.assertLess(branch, src.index('mint_ledger_token "preflight" || exit 1'), "the GitHub mint is the other branch of the same if")
        self.assertLess(branch, src.index('reset_audit_ledgers "lease"'))

    def test_a_pair_that_cannot_be_read_stops_the_run(self):
        self._gcloud(fail="gitlab-ledger-token")
        proc = self._run(self._preamble() + '\nread_gitlab_tokens; echo "RC=$?"\n')
        self.assertIn("RC=1", proc.stdout)
        self.assertIn("could not read kube-agents-prow/gitlab-ledger-token as this runner", proc.stderr)
        self.assertNotIn("gitlab-agent-token", proc.stderr.split("could not read", 1)[1].split(" as this runner")[0])

    def test_the_resets_get_the_agent_token_and_the_forge_flag(self):
        self._gcloud()
        body = self._preamble() + "\n".join(
            [
                "",
                "read_gitlab_tokens >/dev/null",
                'EVAL_LEDGER_REPO="$(eval_gitlab_project "${PROJECT_ID}")"',
                'reset_audit_ledgers "lease"',
                'reset_audit_ledgers "unit rep 1" "compliance-audit"',
                'reset_agent_pulls "lease"; echo "PULLS_RC=$?"',
            ]
        )
        proc = self._run(body)
        out = proc.stdout
        self.assertIn(f'HELPER argv=["--repo", "{PATH}", "--project", "{PROJECT}", "--build", "1", "--forge", "gitlab"]', out)
        self.assertIn(f'HELPER argv=["--repo", "{PATH}", "--project", "{PROJECT}", "--build", "1", "--forge", "gitlab", "--audit", "compliance-audit"]', out)
        record = f"{self.dir}/artifacts/agent-pulls-reset/lease.json"
        self.assertIn(
            f'HELPER argv=["--repo", "{PATH}", "--project", "{PROJECT}", "--build", "1", "--scope", "lease", "--record", "{record}", "--forge", "gitlab"]',
            out,
        )
        self.assertIn("HELPER token=glpat-agent", out)
        self.assertNotIn("glpat-ledger", out.replace("LEDGER=glpat-ledger", ""))
        self.assertIn("PULLS_RC=0", out)

    def test_the_forge_switch_refuses_an_unknown_forge(self):
        src = SCRIPT.read_text(encoding="utf-8")
        start = src.index('EVAL_FORGE="${EVAL_FORGE:-github}"')
        block = src[start : src.index("esac", start) + 4]
        proc = run_bash(block, env={"EVAL_FORGE": "bitbucket"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("EVAL_FORGE='bitbucket' is not a forge", proc.stderr)
        self.assertEqual(run_bash(block, env={"EVAL_FORGE": ""}).returncode, 0)
        # A developer's GitHub-era override is refused under gitlab, where the deploy ignores it.
        overridden = run_bash(block, env={"EVAL_FORGE": "gitlab", "EVAL_GITOPS_REPO": "me/throwaway"})
        self.assertNotEqual(overridden.returncode, 0)
        self.assertIn("does not take EVAL_GITOPS_REPO", overridden.stderr)
        self.assertEqual(run_bash(block, env={"EVAL_FORGE": "gitlab", "EVAL_GITOPS_REPO": ""}).returncode, 0)

    def test_the_mint_is_a_no_op_under_gitlab(self):
        body = "\n".join(
            [
                'EVAL_FORGE="gitlab"; EVAL_LEDGER_APP_KEY_FILE="/k.pem"; EVAL_LEDGER_APP_ID=1; EVAL_LEDGER_INSTALLATION_ID=1',
                "LEDGER_MINT_RETRYABLE=75; LEDGER_MINT_ATTEMPTS=1; LEDGER_GRADING_MINT_BODY='{}'",
                '_ledger_token_mint() { echo "MINTED" >&2; return 1; }',
                lifted("mint_ledger_token"),
                'mint_ledger_token "unit"; echo "RC=$?"',
            ]
        )
        proc = run_bash(body)
        self.assertIn("RC=0", proc.stdout)
        self.assertNotIn("MINTED", proc.stderr)


if __name__ == "__main__":
    unittest.main()
