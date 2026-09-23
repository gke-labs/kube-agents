"""Tests for the ledger-token mint's retry in hack/ci-eval-pr.sh.

`run_one_unit` mints its own installation token after it has taken both locks,
and a unit that cannot mint releases them and returns. That return costs the
repetition its run directory, the fan-out records it `MISSING`, and the gate
grades `MISSING` at rung CHECK_DID_NOT_RUN -- which is blocking, and whose
reason line reads "a harness or agent crash, not infrastructure". So one
unreachable api.github.com reds the whole suite and points the reader at the
agent rather than at the mint.

The retry is what stands between those two facts, and neither of them is
visible from a run where GitHub answers. What has to hold:

* a failure that another attempt could survive is retried, up to a bound;
* a credential fault -- the wrong PEM, the wrong installation -- is not, so it
  is reported on the first attempt rather than three sleeps later, by a caller
  that is holding two locks the whole time;
* an exhausted retry still never falls back to the mounted PAT, which is the
  behaviour #994 added the App for.

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
                '  echo "stub failure" >&2',
                '  return "${outcome}"',
                "}",
                # Retrying for real would put the suite's own wall clock inside
                # the backoff ladder. The delays are asserted separately, off
                # the source, so nothing here depends on them being short.
                "sleep() { :; }",
                'mint_ledger_token "unit-under-test" || echo "MINT_RC=$?"',
                'echo "TOKEN=${BENCH_GITHUB_TOKEN:-}"',
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

    def test_the_retries_are_bounded(self):
        retryable = _retryable_rc()
        attempts = _attempts()
        # The ceiling is asserted against a literal as well as against the
        # behaviour: reading the bound out of the script and then checking the
        # script honours it would pass at any bound, including one that keeps a
        # unit sitting on the task lock and the infra lock for an hour.
        self.assertLessEqual(attempts, 5, "a retrying unit holds both locks the whole time")
        rc, calls, _, _ = self._run([retryable] * (attempts + 3))
        self.assertEqual(1, rc)
        self.assertEqual(attempts, calls)

    def test_an_exhausted_retry_does_not_fall_back_to_the_mounted_pat(self):
        # The whole point of #994: a smoke test that passes on the PAT proves
        # nothing about the App credential it was changed to exercise.
        retryable = _retryable_rc()
        rc, _, err, token = self._run([retryable] * _attempts())
        self.assertEqual(1, rc)
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


# Installed through PYTHONPATH: python imports sitecustomize at startup, so the
# heredoc's urllib.request.urlopen is replaced before the mint runs. The
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
    """The Python half of _ledger_token_mint, run for real against a faked GitHub.

    The retry tests above stub the mint in shell, so the lines that turn
    LEDGER_MINT_BODY into the POST's data and Content-Type never ran under
    test, and a regression there -- `data=mint_data` dropped, the `if
    mint_body:` inverted -- would mint the installation's whole grant (issues:
    write on every pool repository since the ledger reset's grant) and stay
    green. Here the real heredoc signs with a throwaway RSA key and posts to
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

    def _mint(self, call):
        script = "\n".join(
            [
                "set -euo pipefail",
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
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "PYTHONPATH": str(self.tmp),
                    "MINT_CAPTURE_FILE": str(self.capture),
                    "EVAL_LEDGER_APP_KEY_FILE": str(self.key),
                    "EVAL_LEDGER_APP_ID": "4739812",
                    "EVAL_LEDGER_INSTALLATION_ID": "157029058",
                    "BENCH_GITHUB_TOKEN": "the-mounted-pat",
                }
            ),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
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

    def test_no_body_means_no_data_which_is_why_every_caller_sends_one(self):
        # The endpoint's contract: no body, the installation's whole grant.
        # Documented by running it, so the two callers above are read as the
        # deliberate narrowing they are.
        seen, _ = self._mint("_ledger_token_mint >/dev/null")
        self.assertIsNone(seen["data"])
        self.assertIsNone(seen["content_type"])


class LedgerMintContractTest(unittest.TestCase):
    """The two halves of the retry live in different languages.

    The shell decides what it retries; the python inside _ledger_token_mint
    decides what is retryable. A literal written twice would let them drift
    into a mint that retries a wrong PEM three times, or reports a network
    blip as a credential fault.
    """

    def test_the_python_is_handed_the_retryable_code_rather_than_repeating_it(self):
        body = _extract(r"^_ledger_token_mint\(\) \{.*?^\}", "_ledger_token_mint")
        self.assertIn('python3 - "${LEDGER_MINT_RETRYABLE}"', body)
        self.assertIn("retryable = int(sys.argv[1])", body)

    def test_a_credential_answer_from_github_is_terminal(self):
        body = _extract(r"^_ledger_token_mint\(\) \{.*?^\}", "_ledger_token_mint")
        branch = re.search(r"except urllib\.error\.HTTPError.*?^except", body, re.S | re.M)
        self.assertIsNotNone(branch, "could not find the HTTPError branch")
        # Server-side and rate-limited answers retry; every other status, which
        # is where 401 and 404 live, exits terminally.
        self.assertIn("if exc.code >= 500 or exc.code == 429:", branch.group(0))
        self.assertIn("temporary(message)", branch.group(0))
        self.assertIn("sys.exit(message)", branch.group(0))

    def test_an_unreachable_api_is_retryable(self):
        body = _extract(r"^_ledger_token_mint\(\) \{.*?^\}", "_ledger_token_mint")
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
