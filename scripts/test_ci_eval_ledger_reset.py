"""The ledger reset: every repetition of an audit case audits from an empty ledger.

`hack/ci_reset_audit_ledgers.py` closes the open ledger issues in ONE leased
project's GitOps repository, and `hack/ci-eval-pr.sh` calls it at lease time
for every stream and inside `run_one_unit`, under the task lock, for that
unit's stream alone. What has to hold, and is checked here against the code
that ships (the shell lifted out of the script, the helper imported):

  - it never touches any repository but the leased project's: a repository
    that is not `<org>/<PROJECT_ID>-infra` is refused before any call, and
    the token is minted narrowed to that one repository and `issues: write`;
  - it closes ledgers and nothing else: the `agent:audit` label (plus the
    stream's `audit:<id>` when one is named), the `[audit] ` title prefix and
    a `[bot]` author, all three; a pull request is never one;
  - a reset that cannot run says why and the run goes on: no App key, an
    unmapped project, a mint the installation refuses, a helper that fails;
  - the per-unit reset sits after the unit's own mint and before devops-bench,
    inside the task lock, and the write token never reaches
    BENCH_GITHUB_TOKEN, which grading keeps read-only.
"""

import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
HELPER = REPO_ROOT / "hack" / "ci_reset_audit_ledgers.py"

_spec = importlib.util.spec_from_file_location("ci_reset_audit_ledgers", HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)

REPO = "gke-agentic/kube-agents-evals-2-infra"
PROJECT = "kube-agents-evals-2"
BOT = "kube-agents-evals-token-minter[bot]"

# The eight ledger-writing cases and the audit id each grades under; a case
# that writes no ledger has none.
AUDIT_IDS = {
    "ai-security-planted-model-audit": "ai-security-audit",
    "compliance-rbac-overgrant": "compliance-audit",
    "consistency-drift-outlier": "fleet-consistency-drift",
    "consistency-no-environment-label": "fleet-consistency-drift",
    "fleet-cost-idle-pool": "fleet-wide-cost-analysis",
    "obtainability-planted-pdb": "obtainability-audit",
    "stockout-pinned-pool": "stockout-prevention",
    "upgrade-readiness-lagging-cluster": "security-patch-orchestrator",
}


def issue(number, audit_id="compliance-audit", title=None, author=BOT, labels=None, pull=False):
    names = ["agent:audit", f"audit:{audit_id}", "severity:major"] if labels is None else labels
    record = {
        "number": number,
        "title": title if title is not None else f"[audit] Security & RBAC Posture Audit — {number} findings",
        "user": {"login": author},
        "labels": [{"name": name} for name in names],
    }
    if pull:
        record["pull_request"] = {"url": "https://api.github.com/x"}
    return record


class FakeApi:
    """Serves the listing pages and records every write, in order."""

    def __init__(self, pages, fail_patch=()):
        self.pages = list(pages)
        self.calls = []
        self.fail_patch = set(fail_patch)

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return self.pages[page - 1] if page <= len(self.pages) else []
        if method == "PATCH":
            number = int(path.rsplit("/", 1)[1])
            if number in self.fail_patch:
                raise urllib.error.HTTPError(path, 403, "Forbidden", {}, io.BytesIO(b""))
        return {}


def run_reset(api, audit_id=None, dry_run=False, repo=REPO, project=PROJECT):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(helper, "api", api), redirect_stdout(out), redirect_stderr(err):
        unclosed = helper.reset(repo, project, "2102186223282950144", "tok", audit_id, dry_run)
    return unclosed, out.getvalue(), err.getvalue()


class RepositoryGuardTest(unittest.TestCase):
    def test_only_the_leased_projects_repository_passes(self):
        helper.expected_repo(REPO, PROJECT)  # no raise
        for repo, project in [
            ("gke-agentic/kube-agents-evals-3-infra", PROJECT),
            ("gke-agentic/kube-agents-evals-2", PROJECT),
            # The right name in the wrong organisation: the mapping only ever
            # yields gke-agentic, and the helper checks that on its own.
            ("someone-else/kube-agents-evals-2-infra", PROJECT),
            ("/kube-agents-evals-2-infra", PROJECT),
            ("gke-agentic/kube-agents-evals-22-infra", PROJECT),
            ("kube-agents-evals-2-infra", PROJECT),
            (REPO, ""),
        ]:
            with self.subTest(repo=repo, project=project), self.assertRaises(helper.ResetError):
                helper.expected_repo(repo, project)

    def test_a_mismatch_is_refused_before_any_call(self):
        api = FakeApi([[issue(54)]])
        with self.assertRaises(helper.ResetError):
            run_reset(api, repo="gke-agentic/kube-agents-evals-3-infra")
        self.assertEqual(api.calls, [])

    def test_main_refuses_a_mismatch_and_a_missing_token(self):
        api = FakeApi([[issue(54)]])
        with (
            mock.patch.object(helper, "api", api),
            mock.patch.dict(os.environ, {helper.TOKEN_ENV: "tok"}),
            redirect_stderr(io.StringIO()),
        ):
            rc = helper.main(["--repo", "gke-agentic/other-infra", "--project", PROJECT, "--build", "b"])
        self.assertEqual(rc, 2)
        self.assertEqual(api.calls, [])
        env = {k: v for k, v in os.environ.items() if k != helper.TOKEN_ENV}
        with mock.patch.dict(os.environ, env, clear=True), redirect_stderr(io.StringIO()):
            rc = helper.main(["--repo", REPO, "--project", PROJECT, "--build", "b", "--dry-run"])
        self.assertEqual(rc, 2)


class ApiTest(unittest.TestCase):
    def _call(self, raw: bytes):
        response = mock.MagicMock()
        response.read.return_value = raw
        response.__enter__.return_value = response
        with mock.patch.object(helper.urllib.request, "urlopen", return_value=response) as urlopen:
            result = helper.api("GET", "/repos/x/y/issues?page=1", "tok")
        return result, urlopen.call_args[0][0]

    def test_the_token_is_a_bearer_header_and_never_in_the_url(self):
        # An installation token authenticates as Bearer, as every other caller
        # in the tree sends it (scripts/github_api.py, the verifier).
        _, request = self._call(b"[]")
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertNotIn("tok", request.full_url)
        self.assertEqual(request.get_method(), "GET")

    def test_an_empty_body_is_none(self):
        self.assertIsNone(self._call(b"")[0])

    def test_a_body_that_is_not_json_is_a_reported_fault_not_a_traceback(self):
        with self.assertRaises(OSError) as raised:
            self._call(b"<html>maintenance</html>")
        self.assertIn("not JSON", str(raised.exception))


class LedgerSelectionTest(unittest.TestCase):
    def test_a_ledger_is_label_plus_title_plus_bot(self):
        def is_ledger(record, audit_id):
            return helper.not_a_ledger_because(record, audit_id) is None

        self.assertTrue(is_ledger(issue(1), None))
        self.assertTrue(is_ledger(issue(1), "compliance-audit"))
        self.assertFalse(is_ledger(issue(1, title="fix(payments-api): resolve crashloop"), None))
        self.assertFalse(is_ledger(issue(1, author="jayantid"), None))
        self.assertFalse(is_ledger(issue(1, labels=["severity:major"]), None))
        self.assertFalse(is_ledger(issue(1, labels=["agent:audit"]), None), "no stream label")
        self.assertFalse(is_ledger(issue(1, pull=True), None))
        self.assertFalse(is_ledger(issue(1, audit_id="obtainability-audit"), "compliance-audit"))

    def test_a_labelled_issue_that_is_not_a_ledger_is_named_when_left_open(self):
        # The bot's re-read of #1881: "closed 0" said nothing about a labelled
        # issue the helper declined, so a human-authored or mis-titled one
        # looked like an empty repository. Now it is named with the reason.
        api = FakeApi([[issue(7, author="jayantid"), issue(8, title="Security posture"), issue(9)]])
        unclosed, out, err = run_reset(api, dry_run=True)
        self.assertEqual(unclosed, 0)
        self.assertIn("#7 left open, not a ledger: author jayantid is not a [bot] login", out)
        self.assertIn("#8 left open, not a ledger: title does not start with '[audit] '", out)
        self.assertIn("would close 1 open ledger(s)", out)
        self.assertEqual(err, "")
        self.assertEqual(helper.not_a_ledger_because(issue(9), None), None)
        self.assertEqual(helper.not_a_ledger_because(issue(9, pull=True), None), "a pull request")
        self.assertEqual(helper.not_a_ledger_because(issue(9, labels=["agent:audit"]), None), "no audit:<id> label")
        self.assertEqual(
            helper.not_a_ledger_because(issue(9, audit_id="obtainability-audit"), "compliance-audit"),
            "no audit:compliance-audit label",
        )

    def test_the_lease_reset_closes_every_stream_oldest_first(self):
        api = FakeApi(
            [
                [
                    issue(54),
                    issue(51, labels=["agent:delivery-watch"], title="Scheduled-report delivery is failing"),
                    issue(43, audit_id="fleet-wide-cost-analysis", title="[audit] Fleet Waste Audit — 1 finding"),
                    issue(40, title="fix(payments-api): increase memory", labels=[]),
                    issue(19, audit_id="stockout-prevention", title="[audit] Fleet Stockout — 5 findings", pull=True),
                ]
            ]
        )
        unclosed, out, err = run_reset(api)
        self.assertEqual(unclosed, 0)
        writes = [(m, p) for m, p, _ in api.calls if m != "GET"]
        self.assertEqual(
            writes,
            [
                ("POST", f"/repos/{REPO}/issues/43/comments"),
                ("PATCH", f"/repos/{REPO}/issues/43"),
                ("POST", f"/repos/{REPO}/issues/54/comments"),
                ("PATCH", f"/repos/{REPO}/issues/54"),
            ],
        )
        self.assertIn("state=open", api.calls[0][1])
        self.assertIn("labels=agent%3Aaudit&", api.calls[0][1])
        self.assertIn(f"closed 2 open ledger(s) in {REPO} at lease time", out)
        self.assertIn("  #43 [audit] Fleet Waste Audit", out)
        self.assertEqual(err, "")

    def test_the_closing_comment_opens_with_the_marker_the_grader_reads(self):
        comment = helper.closing_comment("b", "at lease time")
        self.assertTrue(comment.startswith(helper.RESET_MARKER + "\n"), comment)
        self.assertIn("eval harness's ledger reset", comment)
        # bench/kube_agents_bench/verifiers.py cannot import this script and
        # this test cannot import the bench package, so the literal is pinned
        # by reading the verifier's source.
        verifier_src = (REPO_ROOT / "bench" / "kube_agents_bench" / "verifiers.py").read_text(encoding="utf-8")
        match = re.search(r'^LEDGER_RESET_MARKER = "(.+)"$', verifier_src, re.MULTILINE)
        self.assertIsNotNone(match, "LEDGER_RESET_MARKER not found in verifiers.py")
        self.assertEqual(match.group(1), helper.RESET_MARKER)

    def test_a_close_is_a_comment_naming_the_build_then_state_closed(self):
        api = FakeApi([[issue(54)]])
        run_reset(api, audit_id="compliance-audit")
        comment = api.calls[1][2]["body"]
        self.assertIn("eval build 2102186223282950144", comment)
        self.assertIn("compliance-audit stream", comment)
        self.assertIn("nothing here was resolved", comment)
        self.assertEqual(api.calls[2][2], {"state": "closed", "state_reason": "not_planned"})

    def test_the_stream_reset_asks_for_that_label_and_keeps_the_others(self):
        # GitHub filters server-side; the client filter still holds if it
        # ever returned a neighbour's ledger.
        api = FakeApi([[issue(54), issue(43, audit_id="fleet-wide-cost-analysis")]])
        unclosed, out, _ = run_reset(api, audit_id="compliance-audit")
        self.assertEqual(unclosed, 0)
        self.assertIn("labels=agent%3Aaudit%2Caudit%3Acompliance-audit", api.calls[0][1])
        self.assertEqual([p for m, p, _ in api.calls if m == "PATCH"], [f"/repos/{REPO}/issues/54"])
        self.assertIn("closed 1 open ledger(s) of the compliance-audit stream", out)

    def test_a_dry_run_writes_nothing(self):
        api = FakeApi([[issue(54), issue(43, audit_id="fleet-wide-cost-analysis")]])
        unclosed, out, _ = run_reset(api, dry_run=True)
        self.assertEqual(unclosed, 0)
        self.assertEqual([m for m, _, _ in api.calls], ["GET"])
        self.assertIn("would close 2 open ledger(s)", out)

    def test_one_close_that_fails_does_not_stop_the_rest(self):
        api = FakeApi([[issue(43, audit_id="fleet-wide-cost-analysis"), issue(54)]], fail_patch={43})
        unclosed, out, err = run_reset(api)
        self.assertEqual(unclosed, 1)
        self.assertIn("#43 did not close", err)
        self.assertIn(("PATCH", f"/repos/{REPO}/issues/54"), [(m, p) for m, p, _ in api.calls])
        self.assertIn("closed 1 open ledger(s)", out)

    def test_listing_pages_until_a_short_page(self):
        first = [issue(n) for n in range(1, helper.PER_PAGE + 1)]
        api = FakeApi([first, [issue(200)]])
        _, out, _ = run_reset(api, dry_run=True)
        self.assertEqual([m for m, _, _ in api.calls], ["GET", "GET"])
        self.assertIn(f"would close {helper.PER_PAGE + 1} open ledger(s)", out)

    def test_an_empty_repository_is_a_quiet_zero(self):
        api = FakeApi([[]])
        unclosed, out, err = run_reset(api)
        self.assertEqual(unclosed, 0)
        self.assertIn("closed 0 open ledger(s)", out)
        self.assertEqual(err, "")


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
    print("HELPER token=" + os.environ.get("LEDGER_RESET_TOKEN", ""))
    sys.exit(int(os.environ.get("HELPER_RC", "0")))
    """
)


class ResetStepTest(unittest.TestCase):
    """reset_audit_ledgers with the mint and the helper stubbed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        (self.dir / "ci_reset_audit_ledgers.py").write_text(STUB_HELPER)
        self.body_file = self.dir / "mint-body"
        self.count_file = self.dir / "mint-count"
        self.count_file.write_text("0")

    def run_step(self, args, key_file="/etc/ledger-app-key/key.pem", repo=REPO, mint_rc=None, helper_rc=0):
        # The mint stub records the body it was asked for and how often it ran;
        # `mint_rc` None means it mints, otherwise it fails with that code.
        mint = "\n".join(
            [
                "_ledger_token_mint() {",
                f'  printf "%s" "${{LEDGER_MINT_BODY:-}}" > "{self.body_file}"',
                f'  echo $(( $(cat "{self.count_file}") + 1 )) > "{self.count_file}"',
                "  echo minted >&2",
                (f"  return {mint_rc}" if mint_rc is not None else '  echo "tok-write 2026-09-22T23:00:00Z"'),
                "}",
                "sleep() { :; }",
            ]
        )
        body = "\n".join(
            [
                lifted_line(r"^LEDGER_MINT_RETRYABLE=\d+$"),
                lifted_line(r"^LEDGER_RESET_MINT_ATTEMPTS=\d+$"),
                lifted_line(r"^LEDGER_RESET_MINT_RETRY_DELAY=\d+$"),
                f'EVAL_LEDGER_APP_ID=4739812; EVAL_LEDGER_APP_KEY_FILE="{key_file}"',
                f'EVAL_LEDGER_REPO="{repo}"; PROJECT_ID="{PROJECT}"; BUILD_ID=2102186223282950144',
                f'SCRIPT_DIR="{self.dir}"',
                mint,
                lifted("ledger_reset_token"),
                lifted("reset_audit_ledgers"),
                "reset_audit_ledgers " + args,
                'echo "RC=$?"',
            ]
        )
        return run_bash(body, {"HELPER_RC": str(helper_rc)})

    def mints(self):
        return int(self.count_file.read_text().strip())

    def test_the_unit_reset_narrows_the_token_and_names_its_stream(self):
        result = self.run_step('"compliance-rbac-overgrant rep 2" compliance-audit')
        self.assertIn("RC=0", result.stdout, result.stderr)
        self.assertEqual(
            json.loads(self.body_file.read_text()),
            {"repositories": ["kube-agents-evals-2-infra"], "permissions": {"issues": "write"}},
        )
        argv = json.loads(re.search(r"HELPER argv=(.*)", result.stdout).group(1))
        self.assertEqual(
            argv,
            ["--repo", REPO, "--project", PROJECT, "--build", "2102186223282950144", "--audit", "compliance-audit"],
        )
        self.assertNotIn("tok-write", " ".join(argv))
        self.assertIn("HELPER token=tok-write", result.stdout)
        self.assertIn("Ledger reset (compliance-rbac-overgrant rep 2): HELPER argv=", result.stdout)
        self.assertNotIn("BENCH_GITHUB_TOKEN", lifted("reset_audit_ledgers") + lifted("ledger_reset_token"))

    def test_the_lease_reset_names_no_stream(self):
        result = self.run_step('"lease"')
        argv = json.loads(re.search(r"HELPER argv=(.*)", result.stdout).group(1))
        self.assertNotIn("--audit", argv)
        self.assertEqual(argv[:2], ["--repo", REPO])

    def test_pat_mode_skips_out_loud_without_minting(self):
        result = self.run_step('"lease"', key_file="")
        self.assertIn("Ledger reset (lease): skipped, EVAL_LEDGER_APP_KEY_FILE is unset", result.stdout)
        self.assertNotIn("HELPER", result.stdout)
        self.assertEqual(self.mints(), 0)
        self.assertIn("RC=0", result.stdout)

    def test_an_unmapped_project_skips_out_loud(self):
        result = self.run_step('"lease"', repo="")
        self.assertIn("maps to no GitOps repository", result.stdout)
        self.assertNotIn("HELPER", result.stdout)
        self.assertEqual(self.mints(), 0)
        self.assertIn("RC=0", result.stdout)

    def test_a_refused_grant_warns_once_and_the_run_goes_on(self):
        # A terminal mint failure (a 422: issues: write not granted) is not
        # retried, the helper never runs, and the caller sees 0.
        result = self.run_step('"stockout-pinned-pool rep 3" stockout-prevention', mint_rc=1)
        self.assertEqual(self.mints(), 1)
        self.assertIn("WARNING: Ledger reset (stockout-pinned-pool rep 3): App 4739812 could not mint issues: write", result.stderr)
        self.assertIn("the stockout-prevention stream keeps whatever ledger is open", result.stderr)
        self.assertNotIn("HELPER", result.stdout)
        self.assertIn("RC=0", result.stdout)

    def test_a_transient_mint_failure_is_retried_once(self):
        retryable = int(lifted_line(r"^LEDGER_MINT_RETRYABLE=\d+$").split("=")[1])
        result = self.run_step('"lease"', mint_rc=retryable)
        self.assertEqual(self.mints(), 2)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("RC=0", result.stdout)

    def test_a_failing_helper_warns_and_the_run_goes_on(self):
        result = self.run_step('"lease"', helper_rc=1)
        self.assertIn("WARNING: Ledger reset (lease): the helper exited 1", result.stderr)
        self.assertIn("RC=0", result.stdout)


class AuditIdTest(unittest.TestCase):
    def test_each_audit_case_names_its_stream_and_a_probe_names_none(self):
        body = "\n".join(
            [
                f'BENCH_DIR="{REPO_ROOT / "bench"}"',
                lifted("ledger_audit_id_for_task"),
            ]
            + [f'echo "{case}=$(ledger_audit_id_for_task ./tasks/{case}/task.yaml)"' for case in AUDIT_IDS]
            + ['echo "probe=$(ledger_audit_id_for_task ./tasks/reliability-pdb-probe/task.yaml)"']
            + ['echo "missing=$(ledger_audit_id_for_task ./tasks/no-such-case/task.yaml)"']
        )
        result = run_bash(body)
        got = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual({k: got[k] for k in AUDIT_IDS}, AUDIT_IDS)
        self.assertEqual(got["probe"], "")
        self.assertEqual(got["missing"], "")
        self.assertEqual(result.stderr, "")

    def test_many_matches_do_not_kill_an_errexit_caller(self):
        # run_one_unit assigns the id under `set -e`; a `sed | head` pipeline
        # here could return SIGPIPE under pipefail once the file has more
        # matches than the pipe buffer holds, and the unit would die unlaunched.
        with tempfile.TemporaryDirectory() as tmp:
            yaml = pathlib.Path(tmp) / "task.yaml"
            yaml.write_text("      type: ledger_issue_contains\n" + "      audit: compliance-audit\n" * 20000)
            body = "\n".join(
                [
                    "set -e",
                    'BENCH_DIR="/nonexistent"',
                    lifted("ledger_audit_id_for_task"),
                    f'audit_id="$(ledger_audit_id_for_task "{yaml}")"',
                    'echo "OK ${audit_id}"',
                ]
            )
            result = run_bash(body)
        self.assertEqual(result.stdout.strip(), "OK compliance-audit")
        self.assertEqual(result.stderr, "")

    def test_the_id_is_the_one_under_the_ledger_check_not_the_first_audit_word(self):
        # kyber775's review of #1881 and review-ledgerreset finding 3: a prompt
        # line starting with `audit:` ahead of the check used to win, and a
        # quoted value came out empty, so the unit reset silently did nothing.
        shapes = {
            "decoy prose": (
                (
                    "prompt: |\n  audit: the fleet, then file it.\n  audit: everything\n"
                    "verification_spec:\n  checks:\n    - check:\n        type: ledger_issue_contains\n"
                    "        audit: compliance-audit\n"
                ),
                "compliance-audit",
            ),
            "double quotes": ('check:\n  type: ledger_issue_contains\n  audit: "obtainability-audit"\n', "obtainability-audit"),
            "single quotes": ("check:\n  type: ledger_issue_contains\n  audit: 'stockout-prevention' # trailing\n", "stockout-prevention"),
            "list item": ("- type: ledger_issue_contains\n  audit: fleet-consistency-drift\n", "fleet-consistency-drift"),
            "type with comment": ("check:\n  type: ledger_issue_contains  # the ledger\n  audit: ai-security-audit\n", "ai-security-audit"),
            "commented check": ("# report_contains, not ledger_issue_contains: a chat probe\n      audit: not-a-stream\n", ""),
            "audit before type": ("check:\n  audit: compliance-audit\n  type: ledger_issue_contains\n", "compliance-audit"),
            "another key between": ("check:\n  type: ledger_issue_contains\n  scope: finding_ids\n  audit: obtainability-audit\n", "obtainability-audit"),
            "blank and comment between": ("check:\n  type: ledger_issue_contains\n\n  # the stream\n  audit: compliance-audit\n", "compliance-audit"),
            "nested key is not the mapping": ("check:\n  type: ledger_issue_contains\n  extra:\n    audit: nested\n  audit: compliance-audit\n", "compliance-audit"),
            "other check type": ("check:\n  type: report_contains\n  audit: not-a-stream\n", ""),
            "no check": ("prompt: audit: something\n", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, (text, want) in shapes.items():
                yaml = pathlib.Path(tmp) / "task.yaml"
                yaml.write_text(text)
                body = "\n".join(['BENCH_DIR="/nonexistent"', lifted("ledger_audit_id_for_task"), f'ledger_audit_id_for_task "{yaml}"'])
                result = run_bash(body)
                with self.subTest(shape=name):
                    self.assertEqual(result.stdout.strip(), want)
                    self.assertEqual(result.stderr, "")

    def test_a_ledger_check_whose_audit_key_is_not_found_says_so(self):
        # The awk reads `audit:` beside `type: ledger_issue_contains`; a
        # check laid out any other way used to yield nothing and the unit
        # skipped its reset without a word. Now the skip is loud.
        with tempfile.TemporaryDirectory() as tmp:
            yaml = pathlib.Path(tmp) / "task.yaml"
            for text in (
                "check:\n  type: ledger_issue_contains\n  scope: finding_ids\n  required_phrases: [x]\n",
                # The audit key after a dedent belongs to another mapping.
                "- check:\n    type: ledger_issue_contains\n- other:\n  audit: compliance-audit\n",
            ):
                yaml.write_text(text)
                body = "\n".join(['BENCH_DIR="/nonexistent"', lifted("ledger_audit_id_for_task"), f'ledger_audit_id_for_task "{yaml}"'])
                result = run_bash(body)
                with self.subTest(text=text):
                    self.assertEqual(result.stdout.strip(), "")
                    self.assertIn("WARNING:", result.stderr)
                    self.assertIn("ledger_issue_contains check but no audit: key in the same mapping as its type:", result.stderr)
                    self.assertIn(str(yaml), result.stderr)

    def test_an_absolute_path_is_read_as_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml = pathlib.Path(tmp) / "task.yaml"
            yaml.write_text("verification_spec:\n  checks:\n    - check:\n        type: ledger_issue_contains\n        audit: fleet-consistency-drift # trailing\n")
            body = "\n".join(['BENCH_DIR="/nonexistent"', lifted("ledger_audit_id_for_task"), f'ledger_audit_id_for_task "{yaml}"'])
            result = run_bash(body)
        self.assertEqual(result.stdout.strip(), "fleet-consistency-drift")


class RepositoryMappingTest(unittest.TestCase):
    def test_the_repository_comes_from_ci_deploys_mapping(self):
        body = "\n".join(
            [
                f'SCRIPT_DIR="{REPO_ROOT / "hack"}"',
                lifted("eval_gitops_repo"),
                'echo "two=$(eval_gitops_repo kube-agents-evals-2)"',
                'echo "thirty=$(eval_gitops_repo kube-agents-evals-30)"',
                'if eval_gitops_repo kube-agents-evals-99 >/dev/null; then echo MAPPED; else echo UNMAPPED; fi',
                'if eval_gitops_repo "" >/dev/null; then echo MAPPED; else echo UNMAPPED; fi',
            ]
        )
        result = run_bash(body)
        self.assertIn("two=gke-agentic/kube-agents-evals-2-infra", result.stdout)
        self.assertIn("thirty=gke-agentic/kube-agents-evals-30-infra", result.stdout)
        self.assertEqual(result.stdout.count("UNMAPPED"), 2, result.stdout)


class CallSiteTest(unittest.TestCase):
    """Where the two resets sit in the script, by its text."""

    def test_the_lease_reset_follows_the_preflight_mint_and_precedes_the_matrix(self):
        src = SCRIPT.read_text(encoding="utf-8")
        preflight = src.index('mint_ledger_token "preflight" || exit 1')
        lease = src.index('reset_audit_ledgers "lease"')
        matrix = src.index("# 6. Task Matrix Execution Loop")
        self.assertLess(preflight, lease)
        self.assertLess(lease, matrix)
        self.assertLess(src.index('EVAL_LEDGER_REPO="$(eval_gitops_repo "${PROJECT_ID:-}"'), lease)

    def test_the_unit_reset_is_after_its_mint_before_devops_bench_inside_the_task_lock(self):
        unit = lifted("run_one_unit")
        mint = unit.index('mint_ledger_token "${name} rep ${rep}"')
        reset = unit.index('reset_audit_ledgers "${name} rep ${rep}" "${audit_id}"')
        launch = unit.index("uv run devops-bench")
        release = unit.index('lock_release "${STATE_DIR}/lock-task-${name}"', launch)
        self.assertLess(mint, reset)
        self.assertLess(reset, launch)
        self.assertLess(launch, release)
        self.assertIn('audit_id="$(ledger_audit_id_for_task "${task}")"', unit)
        # Gated on the case writing a ledger at all.
        self.assertIn('if [ -n "${audit_id}" ]; then', unit)

    def test_a_ledger_writing_unit_holds_its_stream_lock_from_before_the_reset_until_the_run_returns(self):
        # Two cases grade fleet-consistency-drift; their task locks differ, so
        # without this one lane's reset closes the other lane's live ledger
        # and the other's finish lands in this lane's fresh one. The stream
        # lock is taken after the task lock (one order everywhere, no cycle),
        # only when the case names a stream, and released on every exit.
        unit = lifted("run_one_unit")
        task_lock = unit.index('lock_acquire "${STATE_DIR}/lock-task-${name}"')
        stream_lock = unit.index('lock_acquire "${STATE_DIR}/lock-stream-${audit_id}"')
        reset = unit.index('reset_audit_ledgers "${name} rep ${rep}" "${audit_id}"')
        launch = unit.index("uv run devops-bench")
        stream_release = unit.index('lock_release "${STATE_DIR}/lock-stream-${audit_id}"', launch)
        task_release = unit.index('lock_release "${STATE_DIR}/lock-task-${name}"', launch)
        self.assertLess(task_lock, stream_lock)
        self.assertLess(stream_lock, reset)
        self.assertLess(reset, launch)
        self.assertLess(launch, stream_release)
        self.assertLess(stream_release, task_release)
        self.assertIn('if [ -n "${audit_id}" ] && ! lock_acquire "${STATE_DIR}/lock-stream-${audit_id}"', unit)
        # Released on the mint-failure path as well as after the run.
        self.assertEqual(unit.count('[ -n "${audit_id}" ] && lock_release "${STATE_DIR}/lock-stream-${audit_id}"'), 2)
        # The same deadline as the task lock: a holder runs a whole unit.
        self.assertEqual(unit.count('"$(($(unit_delegation_timeout "${name}") + 600))"'), 2)

    def test_two_units_on_one_stream_serialise_and_two_on_different_streams_do_not(self):
        # The lock helpers as shipped, with mkdir as the mutex: the second
        # holder of one stream waits until the first releases; a different
        # stream is not waited on.
        with tempfile.TemporaryDirectory() as tmp:
            body = "\n".join(
                [
                    f'STATE_DIR="{tmp}"',
                    lifted("lock_acquire"),
                    lifted_line(r"^lock_release\(\) \{.*\}$"),
                    'lock_acquire "${STATE_DIR}/lock-stream-fleet-consistency-drift" 30',
                    'lock_acquire "${STATE_DIR}/lock-stream-compliance-audit" 30 && echo "other stream: free"',
                    '( lock_acquire "${STATE_DIR}/lock-stream-fleet-consistency-drift" 4 && echo "same stream: taken" ) || echo "same stream: waited out"',
                    'lock_release "${STATE_DIR}/lock-stream-fleet-consistency-drift"',
                    'lock_acquire "${STATE_DIR}/lock-stream-fleet-consistency-drift" 30 && echo "same stream after release: taken"',
                ]
            )
            result = run_bash(body)
        self.assertEqual(
            result.stdout.splitlines(),
            ["other stream: free", "same stream: waited out", "same stream after release: taken"],
            result.stderr,
        )

    def test_the_grading_mint_pins_its_reads_rather_than_inheriting_the_grant(self):
        # An omitted body on the access-token endpoint yields everything the
        # installation holds, so once issues: write is granted for the reset a
        # bodiless grading mint would hand every unit a write token over every
        # pool repository. The grading mint therefore asks for its reads.
        mint = lifted("_ledger_token_mint")
        self.assertIn('os.environ.get("LEDGER_MINT_BODY", "")', mint)
        self.assertIn("mint_data = None", mint)
        body_line = lifted_line(r"^LEDGER_GRADING_MINT_BODY=.*$")
        body = json.loads(body_line.split("=", 1)[1].strip().strip("'"))
        self.assertEqual(body, {"permissions": {"issues": "read", "pull_requests": "read", "metadata": "read"}})
        self.assertNotIn("repositories", body)
        self.assertIn('LEDGER_MINT_BODY="${LEDGER_GRADING_MINT_BODY}" _ledger_token_mint', lifted("mint_ledger_token"))
        self.assertNotIn("write", body_line)


if __name__ == "__main__":
    unittest.main()
