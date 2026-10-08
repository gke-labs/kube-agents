"""Tests for the ledger-token mint's retry in hack/ci-eval-pr.sh.

`run_one_unit` mints its own installation token after it has taken its locks
(task, stream, infra), and a unit that cannot mint releases them and returns
without a run. Before #2562 that cost the repetition its run directory, the
fan-out recorded it `MISSING`, and the gate graded `MISSING` at rung
CHECK_DID_NOT_RUN -- blocking, with a reason line that reads "a harness or
agent crash, not infrastructure". Two GitHub incidents on 2026-10-07 made
every attempt answer HTTP 500 and redded unrelated pull requests that way.
Now a mint that runs out on a transient failure leaves a record in place of
the run (`record_unit_not_run`) that the gate excludes as infrastructure;
`UnitNotRunTest` below runs the real `run_one_unit` to show it, and
bench/tests/test_unit_not_run.py grades the record through the real gate.

The retry still decides whether the repetition is lost at all, and none of
this is visible from a run where GitHub answers. What has to hold:

* a failure that another attempt could survive is retried, up to a bound;
* a credential fault -- the wrong PEM, the wrong installation -- is not, so it
  is reported on the first attempt rather than three sleeps later, by a caller
  that is holding two locks the whole time;
* an exhausted retry still never falls back to the mounted PAT, which is the
  behaviour #994 added the App for;
* an exhausted retry says it was transient (the retryable code, and the mint's
  last line in LEDGER_MINT_LAST_FAILURE), and a refusal says it was not, because
  only the first is recorded as infrastructure.

The functions are extracted from the script and executed with the network half
stubbed out, so these assertions are against the code that ships.
"""

import json
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL_PR = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_LEDGER_MINT = _REPO_ROOT / "hack" / "ledger_token_mint.py"

# What the stub reports, mirroring _ledger_token_mint's own contract: the
# retryable code is read out of the script rather than written here, because a
# test that supplies both halves of an agreement cannot detect them diverging.
_TERMINAL_RC = 1


def _extract(pattern, what):
    text = _CI_EVAL_PR.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.S | re.M)
    assert match, f"could not find {what} in hack/ci-eval-pr.sh"
    return match.group(0)


def _retryable_rc():
    line = _extract(r"^LEDGER_MINT_RETRYABLE=(\d+)$", "LEDGER_MINT_RETRYABLE")
    return int(line.split("=", 1)[1])


def _attempts():
    line = _extract(r"^LEDGER_MINT_ATTEMPTS=(\d+)$", "LEDGER_MINT_ATTEMPTS")
    return int(line.split("=", 1)[1])


class LedgerMintRetryTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)

    def _run(self, outcomes):
        """Run mint_ledger_token against a stub that returns `outcomes` in turn.

        Each entry is an exit code, or None to mint successfully. Returns
        (returncode, calls, stderr, token) -- `calls` being how many times the
        stub was reached, which is the property the bound is about.
        """
        script = "\n".join(
            [
                "set -euo pipefail",
                _extract(r"^LEDGER_MINT_RETRYABLE=\d+$", "LEDGER_MINT_RETRYABLE"),
                _extract(r"^LEDGER_MINT_ATTEMPTS=\d+$", "LEDGER_MINT_ATTEMPTS"),
                _extract(r"^LEDGER_GRADING_MINT_BODY=[^\n]*$", "LEDGER_GRADING_MINT_BODY"),
                _extract(r"^mint_ledger_token\(\) \{.*?^\}", "mint_ledger_token"),
                # The real function runs in a command substitution, so a shell
                # variable it sets would not survive back into the caller. The
                # count goes in a file for the same reason.
                'echo 0 > "${COUNT_FILE}"',
                "_ledger_token_mint() {",
                # The grading mint must pin its reads: a bodiless mint inherits
                # the installation's whole grant, issues: write included since
                # the ledger reset's grant (2026-09-22).
                '  [ "${LEDGER_MINT_BODY:-}" = "${LEDGER_GRADING_MINT_BODY}" ] || { echo "grading mint did not send its read body" >&2; return 98; }',
                '  local n=$(( $(cat "${COUNT_FILE}") + 1 ))',
                '  echo "${n}" > "${COUNT_FILE}"',
                '  local outcome; outcome="$(sed -n "${n}p" "${OUTCOME_FILE}")"',
                '  if [ "${outcome}" = "ok" ]; then',
                '    echo "tok-stub 2026-09-01T12:00:00Z"',
                "    return 0",
                "  fi",
                '  echo "stub diagnostic before the last line" >&2',
                '  echo "stub failure ${n}" >&2',
                '  return "${outcome}"',
                "}",
                # Retrying for real would put the suite's own wall clock inside
                # the backoff ladder. The delays are asserted separately, off
                # the source, so nothing here depends on them being short.
                "sleep() { :; }",
                'mint_ledger_token "unit-under-test" || echo "MINT_RC=$?"',
                'echo "TOKEN=${BENCH_GITHUB_TOKEN:-}"',
                'echo "LAST_FAILURE=${LEDGER_MINT_LAST_FAILURE:-}"',
            ]
        )
        count_file = self.tmp / "count"
        outcome_file = self.tmp / "outcomes"
        key_file = self.tmp / "ledger.pem"
        key_file.write_text("not a real key -- the mint itself is stubbed\n")
        outcome_file.write_text(
            "".join(("ok" if o is None else str(o)) + "\n" for o in outcomes)
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "COUNT_FILE": str(count_file),
                    "OUTCOME_FILE": str(outcome_file),
                    "EVAL_LEDGER_APP_KEY_FILE": str(key_file),
                    "EVAL_LEDGER_APP_ID": "4739812",
                    "EVAL_LEDGER_INSTALLATION_ID": "157029058",
                    "BENCH_GITHUB_TOKEN": "the-mounted-pat",
                }
            ),
        )
        rc_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("MINT_RC=")]
        token_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("TOKEN=")][-1]
        last_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("LAST_FAILURE=")][-1]
        self.last_failure = last_line.split("=", 1)[1]
        return (
            int(rc_line[-1].split("=", 1)[1]) if rc_line else 0,
            int(count_file.read_text().strip()),
            proc.stderr,
            token_line.split("=", 1)[1],
        )

    def test_a_transient_failure_is_retried_and_the_mint_recovers(self):
        retryable = _retryable_rc()
        rc, calls, _, token = self._run([retryable, retryable, None])
        self.assertEqual(0, rc)
        self.assertEqual(3, calls)
        self.assertEqual("tok-stub", token)

    def test_a_credential_fault_is_reported_on_the_first_attempt(self):
        # Not a bound worth spending: a PEM that is not this App's is not going
        # to become one, and the caller is holding the task lock and the infra
        # lock while it waits to find that out.
        rc, calls, err, token = self._run([_TERMINAL_RC, None, None])
        self.assertEqual(1, rc)
        self.assertEqual(1, calls)
        self.assertIn("could not mint a ledger read token", err)
        self.assertEqual("the-mounted-pat", token)
        # Still the mint's own words for the log, which now pass through a file.
        self.assertIn("stub failure 1", err)
        self.assertEqual("stub failure 1", self.last_failure)

    def test_the_retries_are_bounded(self):
        retryable = _retryable_rc()
        attempts = _attempts()
        # The ceiling is asserted against a literal as well as against the
        # behaviour: reading the bound out of the script and then checking the
        # script honours it would pass at any bound, including one that keeps a
        # unit sitting on the task lock and the infra lock for an hour.
        self.assertLessEqual(attempts, 5, "a retrying unit holds both locks the whole time")
        rc, calls, _, _ = self._run([retryable] * (attempts + 3))
        self.assertEqual(retryable, rc)
        self.assertEqual(attempts, calls)

    def test_an_exhausted_retry_says_it_was_transient_and_keeps_the_last_line(self):
        # What run_one_unit records the repetition as infrastructure on: the
        # retryable code, and the last attempt's diagnostic for the reason.
        retryable = _retryable_rc()
        attempts = _attempts()
        rc, _, _, _ = self._run([retryable] * attempts)
        self.assertEqual(retryable, rc)
        self.assertEqual(f"stub failure {attempts}", self.last_failure)

    def test_a_refusal_after_a_transient_failure_is_not_transient(self):
        # The last answer decides: a 401 on the second attempt is a credential
        # fault, whatever the first attempt hit.
        rc, calls, _, _ = self._run([_retryable_rc(), _TERMINAL_RC, None])
        self.assertEqual(1, rc)
        self.assertEqual(2, calls)

    def test_an_exhausted_retry_does_not_fall_back_to_the_mounted_pat(self):
        # The whole point of #994: a smoke test that passes on the PAT proves
        # nothing about the App credential it was changed to exercise.
        retryable = _retryable_rc()
        rc, _, err, token = self._run([retryable] * _attempts())
        self.assertEqual(retryable, rc)
        self.assertEqual("the-mounted-pat", token)
        self.assertIn("not falling back to the mounted PAT", err)

    def test_no_key_file_means_no_mint_and_no_retry(self):
        # Unset EVAL_LEDGER_APP_KEY_FILE is how a local run keeps using the PAT.
        script = "\n".join(
            [
                "set -euo pipefail",
                _extract(r"^LEDGER_MINT_RETRYABLE=\d+$", "LEDGER_MINT_RETRYABLE"),
                _extract(r"^LEDGER_MINT_ATTEMPTS=\d+$", "LEDGER_MINT_ATTEMPTS"),
                _extract(r"^mint_ledger_token\(\) \{.*?^\}", "mint_ledger_token"),
                '_ledger_token_mint() { echo "the mint must not run" >&2; return 1; }',
                'EVAL_LEDGER_APP_KEY_FILE=""',
                'mint_ledger_token "unit-under-test"',
                'echo "TOKEN=${BENCH_GITHUB_TOKEN:-}"',
            ]
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"BENCH_GITHUB_TOKEN": "the-mounted-pat"}),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("the mint must not run", proc.stderr)
        self.assertIn("TOKEN=the-mounted-pat", proc.stdout)


_SCORING = _REPO_ROOT / "bench" / "kube_agents_bench" / "scoring.py"


def _lift_function(name):
    return _extract(rf"^{name}\(\) \{{.*?^\}}", name)


def _lift_line(pattern, what):
    text = _CI_EVAL_PR.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.M)
    assert match, f"could not find {what} in hack/ci-eval-pr.sh"
    return match.group(0)


# hack/ci_reset_agent_pulls.py as run_one_unit's reset meets it: the exit
# codes and the lines the real helper prints, by RESET_HELPER.
RESET_HELPER_STUB = """\
import os, sys
mode = os.environ.get("RESET_HELPER", "clean")
if mode == "github-500":
    print("  GET /repos/x/pulls answered HTTP 500; trying again in 2s", file=sys.stderr)
    print("ERROR: GitHub answered HTTP 500 Internal Server Error reading gke-agentic/kube-agents-evals-2-infra", file=sys.stderr)
    sys.exit(1)
if mode == "unclean":
    print("closed 0 pull request(s) and deleted 0 branch(es); 0 agent pull request(s) and 1 branch(es) remain")
    sys.exit(1)
print("closed 0 pull request(s) and deleted 0 branch(es); 0 agent pull request(s) and 0 branch(es) remain")
"""


class UnitNotRunTest(unittest.TestCase):
    """The real run_one_unit, with everything around the mint stubbed.

    A unit whose mint runs out on a transient failure writes a record in place
    of the run and counts as a finished repetition; one GitHub refused writes
    nothing and grades MISSING as before; and one whose mint succeeds but whose
    devops-bench writes nothing still grades MISSING -- the crash rung 2 is for.
    The same for the repository reset's mint on a case that requests a pull
    request. What the gate makes of the record is bench/tests/test_unit_not_run.py's.
    """

    maxDiff = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.finished = self.tmp / "finished"
        self.finished.write_text("")
        self.key = self.tmp / "ledger.pem"
        self.key.write_text("not a real key -- the mint itself is stubbed\n")

    def _unit(self, rep, grading_mint, reset_mint=None, phase="0", name="case-under-test", reset_helper="clean"):
        """Run run_one_unit for one repetition.

        grading_mint / reset_mint: "ok", "transient" or "refused" -- what the
        stubbed mint answers on every attempt for that body.
        reset_helper: what the stubbed hack/ci_reset_agent_pulls.py does once
        the reset's mint is good -- "clean", "github-500" (its retries ran out
        on GitHub's answer; it exits 1, as the real helper does) or "unclean"
        (it ran and the read-back found a leftover).
        """
        hack = self.tmp / "hack"
        hack.mkdir(exist_ok=True)
        (hack / "ci_reset_agent_pulls.py").write_text(RESET_HELPER_STUB)
        script = "\n".join(
            [
                "set -euo pipefail",
                _lift_line(r"^readonly EVAL_INFRA_FAILURE_MARKER=.*$", "EVAL_INFRA_FAILURE_MARKER"),
                _lift_line(r"^readonly EVAL_NOT_RUN_DIR=.*$", "EVAL_NOT_RUN_DIR"),
                _lift_line(r"^readonly EVAL_NOT_RUN_STATUS=.*$", "EVAL_NOT_RUN_STATUS"),
                _lift_line(r"^readonly EVAL_INFLIGHT_GRACE_SECONDS=.*$", "EVAL_INFLIGHT_GRACE_SECONDS"),
                _lift_line(r"^readonly EVAL_INJECT_LOCAL_PORT_BASE=.*$", "EVAL_INJECT_LOCAL_PORT_BASE"),
                _extract(r"^LEDGER_MINT_RETRYABLE=\d+$", "LEDGER_MINT_RETRYABLE"),
                _extract(r"^LEDGER_MINT_ATTEMPTS=\d+$", "LEDGER_MINT_ATTEMPTS"),
                _extract(r"^LEDGER_RESET_MINT_ATTEMPTS=\d+$", "LEDGER_RESET_MINT_ATTEMPTS"),
                _extract(r"^LEDGER_RESET_MINT_RETRY_DELAY=\d+$", "LEDGER_RESET_MINT_RETRY_DELAY"),
                _extract(r"^LEDGER_GRADING_MINT_BODY=[^\n]*$", "LEDGER_GRADING_MINT_BODY"),
                _extract(r"^AGENT_PULLS_RESET_PERMISSIONS=[^\n]*$", "AGENT_PULLS_RESET_PERMISSIONS"),
                *(
                    _lift_function(f)
                    for f in (
                        "mint_ledger_token",
                        "ledger_reset_token",
                        "forge_write_token",
                        "reset_agent_pulls",
                        "record_unit_not_run",
                        "finished_rep_count",
                        "release_streams",
                        "skip_unit",
                        "run_one_unit",
                    )
                ),
                # The mint answers by body: the grading mint's reads, or the
                # reset's narrowed write. Every attempt the same answer.
                "_ledger_token_mint() {",
                '  local want="${RESET_MINT}"',
                '  [ "${LEDGER_MINT_BODY}" = "${LEDGER_GRADING_MINT_BODY}" ] && want="${GRADING_MINT}"',
                '  case "${want}" in',
                '    ok) echo "tok-stub 2026-10-07T18:00:00Z" ;;',
                '    transient) echo "GitHub answered HTTP 500 (Internal Server Error) minting for App 4739812 installation 157029058" >&2; return "${LEDGER_MINT_RETRYABLE}" ;;',
                '    *) echo "GitHub answered HTTP 401 (Unauthorized) minting for App 4739812 installation 157029058" >&2; return 1 ;;',
                "  esac",
                "}",
                "sleep() { :; }",
                "lock_acquire() { return 0; }",
                "lock_release() { :; }",
                "ledger_audit_id_for_task() { :; }",
                "stream_case_count() { echo 1; }",
                "stream_stack_wait() { echo 0; }",
                "stream_lock_deadline() { echo 1; }",
                "wait_platform_runs() { :; }",
                "unit_delegation_timeout() { echo 1; }",
                'unit_phase() { echo "${UNIT_PHASE}"; }',
                "unit_task_path() { echo \"$1\"; }",
                "_now_ms() { echo 1700000000000; }",
                "_ts_lines() { cat; }",
                # devops-bench, when it is reached at all, writes nothing:
                # the crash shape.
                'uv() { echo "devops-bench reached" >> "${FINISHED_FILE}"; echo "Traceback: crashed"; }',
                'finish_case() { echo "graded $2" >> "${FINISHED_FILE}"; }',
                'run_one_unit "${TASK_FILE}" "${CASE_NAME}" "${REP}" "" "" 1',
            ]
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "STATE_DIR": str(self.state),
                    "ARTIFACT_DIR": str(self.tmp / "artifacts"),
                    "BENCH_DIR": str(self.tmp),
                    "FINISHED_FILE": str(self.finished),
                    "TASK_FILE": str(self.tmp / "task.yaml"),
                    "CASE_NAME": name,
                    "REP": str(rep),
                    "EVAL_REPETITIONS": "3",
                    "INFRA_LOCK_DEADLINE": "1",
                    "EVAL_CLUSTER_NAME": "c",
                    "EVAL_DEFAULT_LOCATION": "l",
                    "EVAL_LEDGER_APP_KEY_FILE": str(self.key),
                    "EVAL_LEDGER_APP_ID": "4739812",
                    "EVAL_LEDGER_INSTALLATION_ID": "157029058",
                    "EVAL_LEDGER_REPO": "gke-agentic/kube-agents-evals-2-infra",
                    "PROJECT_ID": "kube-agents-evals-2",
                    "GRADING_MINT": grading_mint,
                    "RESET_MINT": reset_mint or "ok",
                    "UNIT_PHASE": phase,
                    "SCRIPT_DIR": str(self.tmp / "hack"),
                    "RESET_HELPER": reset_helper,
                    "TMPDIR": str(self.tmp),
                },
            ),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc

    def _dir(self, rep, name="case-under-test"):
        path = self.state / f"{name}.rep{rep}.dir"
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    def _record(self, rep):
        run_dir = self._dir(rep)
        self.assertTrue(run_dir, "the repetition named no run directory")
        return json.loads((pathlib.Path(run_dir) / "results.json").read_text(encoding="utf-8"))[0]

    def test_a_transient_mint_failure_is_recorded_as_infrastructure(self):
        proc = self._unit(2, "transient")
        record = self._record(2)
        self.assertEqual(1, len(record["errors"]))
        error = record["errors"][0]
        self.assertTrue(error.startswith("KUBE_AGENTS_INFRA_FAILURE: "), error)
        self.assertIn("ledger read token could not be minted before launch", error)
        self.assertIn("GitHub answered HTTP 500", error)
        self.assertEqual([], record["trajectory"])
        self.assertNotIn("devops-bench reached", self.finished.read_text())
        # Rep 2 of 3 with no sibling finished: not the case's last, so ungraded.
        self.assertNotIn("graded case-under-test", self.finished.read_text())
        self.assertIn("could not mint a ledger token", proc.stderr)

    def test_the_last_repetition_not_run_grades_its_case(self):
        # Reps 1 and 2 ran (state files written, as run_one_unit writes them);
        # rep 3's mint fails, and its record is the count's third.
        for rep in (1, 2):
            for suffix in ("start", "end", "dir"):
                (self.state / f"case-under-test.rep{rep}.{suffix}").write_text("1\n")
        self._unit(3, "transient")
        self.assertIn("graded case-under-test", self.finished.read_text())

    def test_a_repetition_not_run_before_the_last_leaves_the_case_ungraded(self):
        # Rep 2 of 3 lost to the mint: its record counts, but rep 3 has not
        # finished, so the case is not graded yet. finish_case is the last
        # repetition's to call, whichever way that one ends.
        (self.state / "case-under-test.rep1.start").write_text("1\n")
        (self.state / "case-under-test.rep1.end").write_text("1\n")
        (self.state / "case-under-test.rep1.dir").write_text("1\n")
        self._unit(2, "transient")
        self.assertTrue(self._record(2)["errors"][0].startswith("KUBE_AGENTS_INFRA_FAILURE: "))
        self.assertNotIn("graded", self.finished.read_text())

    def test_a_refused_mint_stays_missing(self):
        # A credential fault is not weather: no record, no state, MISSING.
        self._unit(2, "refused")
        self.assertIsNone(self._dir(2))
        self.assertFalse((self.state / "not-run").exists())
        self.assertEqual("", self.finished.read_text())

    def test_a_crash_after_a_good_mint_stays_missing(self):
        # The rung-2 shape this must not swallow: devops-bench ran and wrote
        # no results.json, so the repetition's run directory is empty.
        self._unit(2, "ok")
        self.assertIn("devops-bench reached", self.finished.read_text())
        self.assertEqual("", self._dir(2))
        self.assertFalse((self.state / "not-run").exists())

    def test_a_transient_reset_mint_failure_is_recorded_as_infrastructure(self):
        self._unit(1, "ok", reset_mint="transient", phase="1")
        error = self._record(1)["errors"][0]
        self.assertTrue(error.startswith("KUBE_AGENTS_INFRA_FAILURE: "), error)
        self.assertIn("repository reset's token could not be minted before launch", error)
        self.assertIn("GitHub answered HTTP 500", error)
        self.assertNotIn("devops-bench reached", self.finished.read_text())
        self.assertNotIn("graded case-under-test", self.finished.read_text())

    def test_a_refused_reset_mint_stays_missing(self):
        self._unit(1, "ok", reset_mint="refused", phase="1")
        self.assertIsNone(self._dir(1))
        self.assertNotIn("devops-bench reached", self.finished.read_text())

    def test_a_clean_reset_launches_the_unit(self):
        # The stub's happy path, so the two below are about the verdict.
        self._unit(1, "ok", phase="1")
        self.assertIn("devops-bench reached", self.finished.read_text())

    def test_a_reset_that_ran_out_on_github_stays_missing(self):
        # The mint was good and the reset's own reads ran out on GitHub's
        # 500s. Out of #2562's scope, which is the mint: this grades MISSING
        # as on main until the follow-up (#RESETFU) gives it a transient exit.
        self._unit(1, "ok", phase="1", reset_helper="github-500")
        self.assertIsNone(self._dir(1))
        self.assertFalse((self.state / "not-run").exists())
        self.assertNotIn("devops-bench reached", self.finished.read_text())

    def test_a_reset_that_left_the_repository_unclean_stays_missing(self):
        self._unit(1, "ok", phase="1", reset_helper="unclean")
        self.assertIsNone(self._dir(1))
        self.assertFalse((self.state / "not-run").exists())
        self.assertNotIn("devops-bench reached", self.finished.read_text())


class InfraMarkerContractTest(unittest.TestCase):
    def test_the_shell_marker_is_the_scorers(self):
        """The record is only infrastructure if the gate reads this exact word."""
        line = _lift_line(r"^readonly EVAL_INFRA_FAILURE_MARKER=.*$", "EVAL_INFRA_FAILURE_MARKER")
        scorer = re.search(r'^INFRA_FAILURE_MARKER = "([^"]+)"$', _SCORING.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(scorer)
        self.assertEqual(scorer.group(1), line.split("=", 1)[1].strip('"'))

    def test_the_ladder_was_not_lengthened(self):
        # The fix for an incident is the record, not a longer wait: a unit
        # holds its task and stream locks while it retries.
        self.assertLessEqual(_attempts(), 3)


# Installed through PYTHONPATH: python imports sitecustomize at startup, so the
# mint module's urllib.request.urlopen is replaced before the mint runs. The
# request it was handed is written out for the test to read.
_FAKE_URLOPEN = textwrap.dedent(
    '''
    import io
    import json
    import os
    import urllib.request


    def _fake_urlopen(request, timeout=None):
        record = {
            "url": request.full_url,
            "method": request.get_method(),
            "data": request.data.decode() if request.data is not None else None,
            "content_type": request.get_header("Content-type"),
            "auth_scheme": (request.get_header("Authorization") or "").split(" ", 1)[0],
        }
        with open(os.environ["MINT_CAPTURE_FILE"], "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        # A BytesIO is already the context manager `with urlopen(...)` wants.
        return io.BytesIO(
            json.dumps({"token": "ghs_minted", "expires_at": "2026-09-23T16:00:00Z"}).encode()
        )


    urllib.request.urlopen = _fake_urlopen
    '''
)


class LedgerMintRequestTest(unittest.TestCase):
    """hack/ledger_token_mint.py, run for real through _ledger_token_mint against a faked GitHub.

    The retry tests above stub the mint in shell, so the lines that turn
    LEDGER_MINT_BODY into the POST's data and Content-Type never ran under
    test, and a regression there -- `data=mint_data` dropped, the `if
    mint_body:` inverted -- would mint the installation's whole grant (issues:
    write on every pool repository since the ledger reset's grant) and stay
    green. Here the real mint module signs with a throwaway RSA key and posts to
    a urlopen installed through sitecustomize, and the request is asserted.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.key = self.tmp / "throwaway.pem"
        try:
            gen = subprocess.run(
                ["openssl", "genrsa", "-out", str(self.key), "2048"], capture_output=True, text=True
            )
        except FileNotFoundError:  # pragma: no cover - a machine without openssl
            self.skipTest("openssl is not on PATH, and the mint signs its JWT with it")
        if gen.returncode != 0:  # pragma: no cover - an openssl that cannot generate a key
            self.skipTest(f"openssl could not generate a throwaway key: {gen.stderr}")
        (self.tmp / "sitecustomize.py").write_text(_FAKE_URLOPEN, encoding="utf-8")
        self.capture = self.tmp / "request.json"

    def _mint(self, call, expect_rc=0, ids=None):
        """ids: the EVAL_LEDGER_APP_ID / EVAL_LEDGER_INSTALLATION_ID pair to export,
        None for the ledger-reader App's, or {} to export neither."""
        script = "\n".join(
            [
                "set -euo pipefail",
                # The mint module lives beside the script; the extracted
                # constant finds it through the same SCRIPT_DIR the script sets.
                f'SCRIPT_DIR="{_CI_EVAL_PR.parent}"',
                _extract(r"^LEDGER_MINT_SCRIPT=[^\n]*$", "LEDGER_MINT_SCRIPT"),
                _extract(r"^LEDGER_MINT_RETRYABLE=\d+$", "LEDGER_MINT_RETRYABLE"),
                _extract(r"^LEDGER_MINT_ATTEMPTS=\d+$", "LEDGER_MINT_ATTEMPTS"),
                _extract(r"^LEDGER_RESET_MINT_ATTEMPTS=\d+$", "LEDGER_RESET_MINT_ATTEMPTS"),
                _extract(r"^LEDGER_RESET_MINT_RETRY_DELAY=\d+$", "LEDGER_RESET_MINT_RETRY_DELAY"),
                _extract(r"^LEDGER_GRADING_MINT_BODY=[^\n]*$", "LEDGER_GRADING_MINT_BODY"),
                _extract(r"^_ledger_token_mint\(\) \{.*?^\}", "_ledger_token_mint"),
                _extract(r"^mint_ledger_token\(\) \{.*?^\}", "mint_ledger_token"),
                _extract(r"^ledger_reset_token\(\) \{.*?^\}", "ledger_reset_token"),
                "sleep() { :; }",
                call,
            ]
        )
        exported = (
            {"EVAL_LEDGER_APP_ID": "4739812", "EVAL_LEDGER_INSTALLATION_ID": "157029058"}
            if ids is None
            else ids
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "PYTHONPATH": str(self.tmp),
                    "MINT_CAPTURE_FILE": str(self.capture),
                    "EVAL_LEDGER_APP_KEY_FILE": str(self.key),
                    "BENCH_GITHUB_TOKEN": "the-mounted-pat",
                    **exported,
                },
                # A shell that has run the harness exports both ids; an id the
                # caller did not name must be absent, not inherited, or the
                # default case asserts the shell's value.
                absent=[key for key in ("EVAL_LEDGER_APP_ID", "EVAL_LEDGER_INSTALLATION_ID") if key not in exported],
            ),
        )
        self.assertEqual(expect_rc, proc.returncode, proc.stderr)
        if expect_rc != 0:
            return None, proc
        self.assertTrue(self.capture.exists(), "the faked urlopen was never reached: " + proc.stderr)
        return json.loads(self.capture.read_text(encoding="utf-8")), proc

    def test_the_grading_mint_posts_its_three_reads(self):
        seen, proc = self._mint('mint_ledger_token "unit-under-test"; echo "TOKEN=${BENCH_GITHUB_TOKEN}"')
        self.assertEqual(
            {"permissions": {"issues": "read", "pull_requests": "read", "metadata": "read"}},
            json.loads(seen["data"]),
        )
        self.assertEqual("application/json", seen["content_type"])
        self.assertEqual("POST", seen["method"])
        self.assertIn("/app/installations/157029058/access_tokens", seen["url"])
        self.assertEqual("Bearer", seen["auth_scheme"])
        self.assertIn("TOKEN=ghs_minted", proc.stdout)

    def test_the_reset_mint_posts_one_repository_and_issues_write(self):
        seen, proc = self._mint(
            'tok="$(ledger_reset_token gke-agentic/kube-agents-evals-2-infra)"; echo "RESET=${tok}"'
        )
        self.assertEqual(
            {"repositories": ["kube-agents-evals-2-infra"], "permissions": {"issues": "write"}},
            json.loads(seen["data"]),
        )
        self.assertEqual("application/json", seen["content_type"])
        self.assertIn("RESET=ghs_minted", proc.stdout)

    def test_the_ids_come_from_the_environment_when_it_sets_them(self):
        seen, _ = self._mint(
            'mint_ledger_token "unit-under-test"',
            ids={"EVAL_LEDGER_APP_ID": "424242", "EVAL_LEDGER_INSTALLATION_ID": "515151"},
        )
        self.assertIn("/app/installations/515151/access_tokens", seen["url"])

    def test_the_ids_default_to_the_ledger_reader_app(self):
        """Step 0 (hack/ci-revalidate.sh) exports neither id and calls the
        module with a body alone, so what it mints with is the module's own
        default -- which test_verify_ci_pool_project holds to the verifier's
        and the harness's copies. mint_ledger_token itself logs the exported
        ids, so the step-0 shape is the module through _ledger_token_mint."""
        seen, _ = self._mint(
            'LEDGER_MINT_BODY="${LEDGER_GRADING_MINT_BODY}" _ledger_token_mint >/dev/null', ids={}
        )
        self.assertIn("/app/installations/157029058/access_tokens", seen["url"])

    def test_a_bodiless_mint_is_refused_rather_than_sent(self):
        # The endpoint's contract: no body, the installation's whole grant --
        # issues: write on every pool repository. A caller that forgets the
        # body must fail to mint, terminally, and never reach GitHub.
        _, proc = self._mint("_ledger_token_mint >/dev/null", expect_rc=_TERMINAL_RC)
        self.assertIn("LEDGER_MINT_BODY is empty; refusing to mint", proc.stderr)
        self.assertFalse(self.capture.exists(), "a bodiless mint reached the faked GitHub")
        self.assertNotEqual(_retryable_rc(), proc.returncode, "an empty body is not a transient fault")


# A GitHub that refuses the mint: the HTTPError the test names (status,
# headers, body) is raised from urlopen, as urllib raises GitHub's own.
_FAKE_REFUSAL = textwrap.dedent(
    '''
    import email.message
    import io
    import json
    import os
    import urllib.error
    import urllib.request


    def _refusing_urlopen(request, timeout=None):
        spec = json.loads(os.environ["MINT_FAKE_REFUSAL"])
        headers = email.message.Message()
        for name, value in spec.get("headers", {}).items():
            headers[name] = value
        raise urllib.error.HTTPError(
            request.full_url, spec["code"], spec.get("reason", "Forbidden"), headers,
            io.BytesIO(spec.get("body", "").encode()),
        )


    urllib.request.urlopen = _refusing_urlopen
    '''
)


class LedgerMintRefusalTest(unittest.TestCase):
    """hack/ledger_token_mint.py's exit code for each kind of refusal, run for real.

    The code is the whole contract: the retryable one is retried and, run out,
    recorded as infrastructure; 1 is a refusal and grades MISSING, which blocks.
    GitHub answers a burst from one installation with a 403 it marks as a
    secondary rate limit (Retry-After, X-RateLimit-Remaining: 0, or a body
    naming the limit), which nothing has to fix; an unmarked 403 (a suspended
    installation) does, and stays terminal. The marks are the ones
    hack/ci_sweep_agent_pulls.py's is_rate_limited reads.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.key = self.tmp / "throwaway.pem"
        try:
            gen = subprocess.run(
                ["openssl", "genrsa", "-out", str(self.key), "2048"], capture_output=True, text=True
            )
        except FileNotFoundError:  # pragma: no cover - a machine without openssl
            self.skipTest("openssl is not on PATH, and the mint signs its JWT with it")
        if gen.returncode != 0:  # pragma: no cover - an openssl that cannot generate a key
            self.skipTest(f"openssl could not generate a throwaway key: {gen.stderr}")
        (self.tmp / "sitecustomize.py").write_text(_FAKE_REFUSAL, encoding="utf-8")

    def _rc(self, code, headers=None, body=""):
        proc = subprocess.run(
            ["python3", str(_LEDGER_MINT), str(_retryable_rc())],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "PYTHONPATH": str(self.tmp),
                    "EVAL_LEDGER_APP_KEY_FILE": str(self.key),
                    "LEDGER_MINT_BODY": '{"permissions": {"issues": "read"}}',
                    "MINT_FAKE_REFUSAL": json.dumps({"code": code, "headers": headers or {}, "body": body}),
                },
            ),
        )
        self.assertIn("GitHub answered HTTP %d" % code, proc.stderr)
        self.assertEqual("", proc.stdout, "a refused mint printed a token")
        return proc.returncode

    def test_a_secondary_rate_limit_body_is_transient(self):
        body = '{"message":"You have exceeded a secondary rate limit. Please wait a few minutes before you try again."}'
        self.assertEqual(_retryable_rc(), self._rc(403, body=body))

    def test_a_403_with_retry_after_is_transient(self):
        self.assertEqual(_retryable_rc(), self._rc(403, headers={"Retry-After": "60"}))

    def test_a_403_with_no_requests_remaining_is_transient(self):
        self.assertEqual(_retryable_rc(), self._rc(403, headers={"x-ratelimit-remaining": "0"}))

    def test_an_unmarked_403_is_terminal(self):
        body = '{"message":"This installation has been suspended"}'
        self.assertEqual(_TERMINAL_RC, self._rc(403, headers={"x-ratelimit-remaining": "4999"}, body=body))

    def test_a_marked_401_is_still_terminal(self):
        # The marks free a 403 only: a wrong PEM is a wrong PEM whatever else
        # the answer carries.
        self.assertEqual(_TERMINAL_RC, self._rc(401, headers={"Retry-After": "60"}, body="rate limit"))

    def test_a_429_and_a_500_are_transient(self):
        self.assertEqual(_retryable_rc(), self._rc(429))
        self.assertEqual(_retryable_rc(), self._rc(500))

    def test_the_marks_are_the_sweepers(self):
        # Two copies of one discriminator: the mint keeps its own so a
        # credential path imports nothing else from hack/, and this holds it to
        # hack/ci_sweep_agent_pulls.py's, which tests/test_ci_sweep_agent_pulls.py covers.
        sweeper = (_REPO_ROOT / "hack" / "ci_sweep_agent_pulls.py").read_text(encoding="utf-8")
        mint = _LEDGER_MINT.read_text(encoding="utf-8")
        for name in ("RATE_LIMIT_BODY_MARKERS", "RETRY_AFTER_HEADER", "RATELIMIT_REMAINING_HEADER"):
            line = re.search(r"^%s = .*$" % name, sweeper, re.M)
            self.assertIsNotNone(line, "the sweeper no longer defines %s" % name)
            self.assertIn(line.group(0), mint, "the mint's %s drifted from the sweeper's" % name)


class LedgerMintContractTest(unittest.TestCase):
    """The two halves of the retry live in different languages.

    The shell decides what it retries; the python in hack/ledger_token_mint.py
    decides what is retryable. A literal written twice would let them drift
    into a mint that retries a wrong PEM three times, or reports a network
    blip as a credential fault.
    """

    def test_the_python_is_handed_the_retryable_code_rather_than_repeating_it(self):
        body = _extract(r"^_ledger_token_mint\(\) \{.*?^\}", "_ledger_token_mint")
        self.assertIn('python3 "${LEDGER_MINT_SCRIPT}" "${LEDGER_MINT_RETRYABLE}"', body)
        self.assertIn("retryable = int(sys.argv[1])", _LEDGER_MINT.read_text(encoding="utf-8"))

    def test_a_credential_answer_from_github_is_terminal(self):
        body = _LEDGER_MINT.read_text(encoding="utf-8")
        branch = re.search(r"except urllib\.error\.HTTPError.*?^except", body, re.S | re.M)
        self.assertIsNotNone(branch, "could not find the HTTPError branch")
        # Server-side and rate-limited answers (a 429, or a 403 marked as the
        # limit) retry; every other status, which is where 401, 404 and an
        # unmarked 403 live, exits terminally.
        self.assertIn("if exc.code >= 500 or exc.code == 429 or rate_limited_403(exc):", branch.group(0))
        self.assertIn("temporary(message)", branch.group(0))
        self.assertIn("sys.exit(message)", branch.group(0))

    def test_an_unreachable_api_is_retryable(self):
        body = _LEDGER_MINT.read_text(encoding="utf-8")
        branch = re.search(r"^except Exception as exc:.*?^print\(", body, re.S | re.M)
        self.assertIsNotNone(branch, "could not find the catch-all branch")
        self.assertIn("temporary(", branch.group(0))
        self.assertNotIn("sys.exit(", branch.group(0))

    def test_the_backoff_grows(self):
        # Three attempts 2s and 8s apart. A flat ladder would hit a rate limit
        # with the same spacing that produced it, and a longer one would sit
        # inside a unit that is holding both locks.
        body = _extract(r"^mint_ledger_token\(\) \{.*?^\}", "mint_ledger_token")
        self.assertIn("delay=2", body)
        self.assertIn("delay=$((delay * 4))", body)
        self.assertGreaterEqual(_attempts(), 2)


if __name__ == "__main__":
    unittest.main()
