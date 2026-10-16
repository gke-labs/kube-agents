"""The eval job runs the matrix through the inject door only under EVAL_MODE_NEXT=1.

`hack/ci-eval-pr.sh` exports `AGENT_TRANSPORT=inject` and the door's bearer
token, read from the Secret the operator renders beside the door, when the
deploy ran under the flag; unset, section 4 fetches the agent token as it
always has and exports neither. Every pull request runs the unset path, so
that is the half pinned first, with five spellings of "not 1". The set half
is run against a stubbed `kubectl` and checked for the exact Secret and key
the operator writes and the harness reads -- three sources that have to agree
on two strings, held together here rather than in three places by hand.

Lifted from the shipped script and run under bash, the way
tests/test_ci_deploy_mode_next.py does for the deploy, rather than grepped: a
guard that greps passes for a section someone has commented out.
"""

import base64
import pathlib
import re
import subprocess
import textwrap
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_A2A_MANIFESTS = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_a2a_manifests.go"
_HARNESS = _REPO_ROOT / "bench" / "kube_agents_bench" / "harness.py"

_SECTION = (r"^# 4\. Token & Model Configuration\n.*?", r"^export JUDGE_API_KEY=")
_FLAG_UNSET_SPELLINGS = (None, "", "0", "true", "yes")
_NAMESPACE = "kubeagents-system"
_AGENT_TOKEN = "agent-api-key"
_INJECT_TOKEN = "door-bearer-token"

# A kubectl that answers the two Secret reads the section can make, with the
# encoded values the real API returns for a jsonpath into .data, and records
# every call.
_KUBECTL_STUB = textwrap.dedent(
    f"""\
    kubectl() {{
      echo "KUBECTL $*" >&2
      case "$*" in
        *"secret platform-agent-secrets "*) printf '%s' '{base64.b64encode(_AGENT_TOKEN.encode()).decode()}' ;;
        *"secret platform-agent-a2a-inject "*) [ -n "${{INJECT_SECRET_MISSING:-}}" ] && return 1; printf '%s' '{base64.b64encode(_INJECT_TOKEN.encode()).decode()}' ;;
        *) echo "unexpected kubectl call: $*" >&2; return 1 ;;
      esac
    }}
    """
)


def text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def section() -> str:
    match = re.search(rf"{_SECTION[0]}(?={_SECTION[1]})", text(_CI_EVAL), re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a re-banner should say so loudly
        raise AssertionError(f"section 4 not found in {_CI_EVAL} between {_SECTION}")
    return match.group(0)


def constants_block() -> str:
    return "\n".join(line for line in text(_CI_EVAL).splitlines() if line.startswith("readonly "))


def lifted_between(start: str, stop: str, what: str) -> str:
    """The text from the first line equal to `start` through the first line
    equal to `stop` after it, both included."""
    lines = text(_CI_EVAL).splitlines()
    for index, line in enumerate(lines):
        if line == start:
            for end in range(index, len(lines)):
                if lines[end] == stop:
                    return "\n".join(lines[index : end + 1])
            break
    raise AssertionError(f"{what} not found in {_CI_EVAL}")


def recorder_decision() -> str:
    """EVAL_IS_MAIN_RUN, decided above the fan-out: the JOB_TYPE case and the
    overrides after it, through the flag's."""
    lines = text(_CI_EVAL).splitlines()
    start = lines.index('case "${JOB_TYPE:-}" in')
    for end in range(start, len(lines) - 2):
        if (
            lines[end] == 'if [ "${EVAL_MODE_NEXT:-}" = "1" ]; then'
            and lines[end + 1] == '  EVAL_IS_MAIN_RUN="false"'
            and lines[end + 2] == "fi"
        ):
            return "\n".join(lines[start : end + 3])
    raise AssertionError(f"the recorder decision not found in {_CI_EVAL}")


def publisher_gate() -> str:
    """publish_eval_dashboard(), as written."""
    match = re.search(r"^publish_eval_dashboard\(\) \{\n.*?^\}$", text(_CI_EVAL), re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"publish_eval_dashboard() not found in {_CI_EVAL}")
    return match.group(0)


def rollback_function() -> str:
    """run_rollback_roundtrip(), as written."""
    match = re.search(r"^run_rollback_roundtrip\(\) \{\n.*?^\}$", text(_CI_EVAL), re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"run_rollback_roundtrip() not found in {_CI_EVAL}")
    return match.group(0)


def recorder_step_message() -> str:
    """The record step after the fan-out: the if/elif/else that either
    records or says why it did not."""
    return lifted_between('if [ "${EVAL_IS_MAIN_RUN}" = "true" ] && [ -z "${PULL_NUMBER:-}" ]; then', "fi", "the record step")


def constants() -> dict[str, str]:
    found = {}
    for line in text(_CI_EVAL).splitlines():
        match = re.match(r"readonly ([A-Z0-9_]+)=(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip("\"'")
    return found


def python_constant(name: str) -> str:
    match = re.search(rf'^{name}\s*=\s*"([^"]*)"', text(_HARNESS), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} not found in {_HARNESS}")
    return match.group(1)


def go_constant(name: str) -> str:
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', text(_A2A_MANIFESTS), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} not found in {_A2A_MANIFESTS}")
    return match.group(1)


def run_section(mode_next: str | None, secret_missing: bool = False) -> subprocess.CompletedProcess:
    script = "\n".join(
        [
            "set -euo pipefail",
            # "Not exported by the test" has to mean unset, not whatever the
            # shell running the tests happens to export: the flag, and the two
            # variables the flag-set path exports, which a developer who drove
            # the door by hand has in their shell.
            "unset EVAL_MODE_NEXT AGENT_TRANSPORT AGENT_INJECT_TOKEN",
            f'export TARGET_NAMESPACE="{_NAMESPACE}"',
            'export AGENT_SERVICE_NAME="platform-agent"',
            "" if mode_next is None else f'export EVAL_MODE_NEXT="{mode_next}"',
            'export INJECT_SECRET_MISSING="1"' if secret_missing else "",
            _KUBECTL_STUB,
            constants_block(),
            section(),
            'echo "TOKEN=${PLATFORM_AGENT_TOKEN:-<unset>}"',
            'echo "TRANSPORT=${AGENT_TRANSPORT:-<unset>}"',
            'echo "INJECT_TOKEN=${AGENT_INJECT_TOKEN:-<unset>}"',
        ]
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def exported(result: subprocess.CompletedProcess) -> dict[str, str]:
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line and not line.startswith("EVAL_MODE_NEXT"))


def kubectl_calls(result: subprocess.CompletedProcess) -> list[str]:
    return [line.removeprefix("KUBECTL ") for line in result.stderr.splitlines() if line.startswith("KUBECTL ")]


class FlagUnsetIsTodayTest(unittest.TestCase):
    def test_the_matrix_runs_over_the_agent_api_with_one_secret_read(self) -> None:
        for value in _FLAG_UNSET_SPELLINGS:
            with self.subTest(EVAL_MODE_NEXT=value):
                result = run_section(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                out = exported(result)
                self.assertEqual(out["TOKEN"], _AGENT_TOKEN)
                self.assertEqual(out["TRANSPORT"], "<unset>")
                self.assertEqual(out["INJECT_TOKEN"], "<unset>")
                calls = kubectl_calls(result)
                self.assertEqual(len(calls), 1, calls)
                self.assertIn("secret platform-agent-secrets", calls[0])

    def test_the_flag_is_read_in_section_4_the_recorder_the_publisher_and_the_rollback_and_nowhere_else(self) -> None:
        """The header promises four places: section 4, the baseline recorder
        (its decision and the log line at the record step), the dashboard
        publisher's gate and the rollback round trip. Another reader is another
        behaviour under the flag that this test suite does not cover. The
        publisher's gate is run in scripts/test_eval_dashboard_publish.py, the
        rollback round trip in tests/test_rollback_roundtrip.py; the others
        here."""
        script = text(_CI_EVAL)
        # Any spelling of an expansion (`${EVAL_MODE_NEXT...}` or bare `$EVAL_MODE_NEXT`),
        # on a line that is not a comment; the log lines that name the flag as
        # text are not reads.
        reads = [
            m.start()
            for m in re.finditer(r"^[^#\n]*\$\{?EVAL_MODE_NEXT\b", script, re.MULTILINE)
        ]
        self.assertEqual(len(reads), 5, "EVAL_MODE_NEXT is read at a site in hack/ci-eval-pr.sh this suite does not cover")
        publisher = script.index(publisher_gate())
        self.assertTrue(publisher <= reads[0] < publisher + len(publisher_gate()))
        start = script.index(section())
        self.assertTrue(start <= reads[1] < start + len(section()))
        decision = script.index(recorder_decision())
        self.assertTrue(decision <= reads[2] < decision + len(recorder_decision()))
        step = script.index(recorder_step_message())
        self.assertTrue(step <= reads[3] < step + len(recorder_step_message()))
        rollback = script.index(rollback_function())
        self.assertTrue(rollback <= reads[4] < rollback + len(rollback_function()))


class FlagSetIsInjectTest(unittest.TestCase):
    def test_the_matrix_runs_through_the_door_with_its_token(self) -> None:
        result = run_section("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = exported(result)
        self.assertEqual(out["TOKEN"], _AGENT_TOKEN, "the agent token is still fetched")
        self.assertEqual(out["TRANSPORT"], "inject")
        self.assertEqual(out["INJECT_TOKEN"], _INJECT_TOKEN)
        calls = kubectl_calls(result)
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(
            calls[1],
            f"get secret platform-agent-a2a-inject -n {_NAMESPACE} -o jsonpath={{.data.token}}",
        )

    def test_the_inject_tunnels_base_is_the_harnesss_variable_and_clear_of_the_api_range(self) -> None:
        """The harness owns one port-forward per process and tears it down at
        exit, which is why run_one_unit gives each unit its own agent-API port;
        the inject door reads a different variable, so it needs the same
        treatment or every unit rides the first one's listener. That the unit
        exports it, per unit, is run rather than read in
        scripts/test_ci_eval_fanout.py (the fan-out's own suite, which runs
        run_one_unit); what is pinned here is the name the harness reads and
        the base the export is built from."""
        harness = text(_HARNESS)
        self.assertIn('"AGENT_INJECT_LOCAL_PORT"', harness)
        base = int(constants()["EVAL_INJECT_LOCAL_PORT_BASE"])
        default = int(re.search(r"^_INJECT_DEFAULT_LOCAL_PORT = (\d+)$", harness, re.MULTILINE).group(1))
        self.assertNotEqual(base, default, "a base equal to the harness default hides a unit that lost the export")
        # The two per-unit ranges cannot meet for any seq the matrix can reach.
        self.assertGreater(abs(base - 28642), 400)

    def test_a_missing_token_secret_stops_the_run_before_the_matrix(self) -> None:
        result = run_section("1", secret_missing=True)
        self.assertNotEqual(result.returncode, 0)
        # The script's own line, not the stub's echo of the kubectl call.
        errors = [line for line in result.stderr.splitlines() if line.startswith("ERROR:")]
        self.assertEqual(len(errors), 1, result.stderr)
        self.assertIn("platform-agent-a2a-inject", errors[0])
        self.assertIn("EVAL_MODE_NEXT=1", errors[0])
        self.assertNotIn("TRANSPORT=", result.stdout)

    def test_the_secret_and_transport_names_are_the_operators_and_the_harnesss(self) -> None:
        consts = constants()
        self.assertEqual(consts["EVAL_INJECT_TRANSPORT"], python_constant("TRANSPORT_INJECT"))
        self.assertEqual(consts["EVAL_INJECT_TOKEN_SECRET_SUFFIX"], python_constant("_INJECT_TOKEN_SECRET_SUFFIX"))
        self.assertEqual(consts["EVAL_INJECT_TOKEN_SECRET_KEY"], python_constant("_INJECT_TOKEN_SECRET_KEY"))
        self.assertEqual(consts["EVAL_INJECT_TOKEN_SECRET_KEY"], go_constant("a2aInjectTokenKey"))
        self.assertRegex(
            text(_A2A_MANIFESTS),
            rf'func a2aInjectName\(.*\) string\s*{{\s*return agent\.Name \+ "{re.escape(consts["EVAL_INJECT_TOKEN_SECRET_SUFFIX"])}"',
        )
        # The harness reads the token from AGENT_INJECT_TOKEN and the switch from AGENT_TRANSPORT.
        harness = text(_HARNESS)
        self.assertIn('os.environ.get("AGENT_INJECT_TOKEN", "")', harness)
        self.assertIn('os.environ.get("AGENT_TRANSPORT", TRANSPORT_API)', harness)
        block = section()
        self.assertIn("export AGENT_INJECT_TOKEN", block)
        self.assertIn('export AGENT_TRANSPORT="${EVAL_INJECT_TRANSPORT}"', block)


class FlagSetNeverRecordsBaselineTest(unittest.TestCase):
    """A flagged run never appends to the baseline store. The next lane's
    periodic on main is JOB_TYPE=periodic with no PULL_NUMBER, exactly the
    shape the recorder appends from, and VersionKey has no mode field, so its
    samples would be today's once written. With the flag unset the decision
    is what it was: main-branch job types record, a release candidate and
    a pull request do not."""

    def decide(self, mode_next: str | None, job_type: str, rc: str = "") -> str:
        script = "\n".join(
            [
                "set -euo pipefail",
                "unset EVAL_MODE_NEXT JOB_TYPE RC_COMMIT_SHA PULL_NUMBER",
                "" if mode_next is None else f'export EVAL_MODE_NEXT="{mode_next}"',
                f'export JOB_TYPE="{job_type}"',
                f'export RC_COMMIT_SHA="{rc}"',
                recorder_decision(),
                'echo "MAIN=${EVAL_IS_MAIN_RUN}"',
            ]
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().removeprefix("MAIN=")

    def test_the_flag_unset_records_from_main_branch_job_types_only(self) -> None:
        for value in _FLAG_UNSET_SPELLINGS:
            with self.subTest(EVAL_MODE_NEXT=value):
                self.assertEqual(self.decide(value, "periodic"), "true")
                self.assertEqual(self.decide(value, "postsubmit"), "true")
                self.assertEqual(self.decide(value, "presubmit"), "false")
                self.assertEqual(self.decide(value, ""), "false")
                self.assertEqual(self.decide(value, "periodic", rc="abc1234"), "false")

    def test_the_flag_set_records_from_nothing(self) -> None:
        for job_type in ("periodic", "postsubmit", "presubmit", ""):
            with self.subTest(JOB_TYPE=job_type):
                self.assertEqual(self.decide("1", job_type), "false")

    def test_the_record_step_says_why_a_flagged_periodic_did_not_record(self) -> None:
        """The record step's else-chain, run with the recorder stubbed: a
        flagged periodic logs the next-mode line and never calls bench-gate;
        without the flag the same periodic records; a flagged presubmit keeps
        the line PULL_NUMBER already earned it."""
        for mode_next, job_type, pull, expect_record, expect_line in (
            ("1", "periodic", "", False, "Next-mode run (EVAL_MODE_NEXT=1, JOB_TYPE=periodic): the baseline store is read, never written"),
            (None, "periodic", "", True, "Recording baseline evidence from main"),
            ("1", "presubmit", "1686", False, "Not a main-branch recorder run (JOB_TYPE=presubmit)"),
        ):
            with self.subTest(EVAL_MODE_NEXT=mode_next, JOB_TYPE=job_type, PULL_NUMBER=pull):
                script = "\n".join(
                    [
                        "set -euo pipefail",
                        "unset EVAL_MODE_NEXT RC_COMMIT_SHA PULL_NUMBER",
                        "" if mode_next is None else f'export EVAL_MODE_NEXT="{mode_next}"',
                        f'export JOB_TYPE="{job_type}"',
                        "" if not pull else f'export PULL_NUMBER="{pull}"',
                        'BENCH_DIR="." ARTIFACT_DIR="." EVAL_RECORD_COMMIT="" EVAL_RECORDED_MANIFEST="./manifest"',
                        "CASE_RESULTS=(./case-a.json)",
                        'uv() { echo "UV $*"; }',
                        recorder_decision(),
                        recorder_step_message(),
                    ]
                )
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expect_line, result.stdout)
                self.assertEqual("UV run bench-gate record" in result.stdout, expect_record, result.stdout)


if __name__ == "__main__":
    unittest.main()
