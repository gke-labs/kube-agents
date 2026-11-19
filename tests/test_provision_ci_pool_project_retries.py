"""The two retried gcloud steps of scripts/provision_ci_pool_project.sh, executed.

Step 1.5 binds a role to the forge VM account it created a moment earlier,
which IAM refuses as "does not exist" until it has seen the account; step 2.1
sets the host cluster's managed-OpenTelemetry scope, which GKE refuses while
another operation holds the cluster. Each section is lifted out of the script
by its markers and run against a stub `gcloud` that records every call and
answers what the case set, so the loops' classification (retry this error,
stop on any other, give up after the budget) is tested without a project.
The delays are overridden to zero; the attempt counts are the script's own.
"""

import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "provision_ci_pool_project.sh"

# The constants the two sections read, lifted verbatim so a renamed or
# retyped constant fails here rather than at the first live run. The two
# delays are left out and defined by the test as zero.
_CONSTANT_RE = re.compile(
    r"^readonly (?:FORGE_VM_SA_ID|FORGE_VM_SA_ROLE|FORGE_VM_BIND_ATTEMPTS|FORGE_VM_BIND_RETRY_PATTERN"
    r"|FORGE_DNS_ZONE|FORGE_FIREWALL_RULE|FORGE_DENY_RULE"
    r"|HOST_OTEL_SCOPE|HOST_OTEL_WAIT_ATTEMPTS|HOST_OTEL_BUSY_PATTERN)=.*$",
    re.MULTILINE,
)
_HOST_NAME_RE = re.compile(r'^HOST_CLUSTER_NAME="platform-agent-host"$', re.MULTILINE)
_FORGE_START = 'FORGE_VM_SA="${FORGE_VM_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"'
_FORGE_END = 'echo "✓ Forge pieces:'
_OTEL_START = 'echo -e "\\n==> [Step 2.1] Setting the managed OpenTelemetry scope'
_OTEL_END = 'echo "✓ Managed OpenTelemetry scope'

# Literals on purpose: the sections under test define the same values, and a
# test that read them back would assert nothing.
_FORGE_ATTEMPTS = 6
_OTEL_ATTEMPTS = 40
_PROJECT = "kube-agents-evals-99"
_REGION = "us-central1"
_HOST = "platform-agent-host"
_QUOTED_FILTER_RE = r"targetLink~'/clusters/platform-agent-host\(/\|\$\)'"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
# Records every call, one JSON array per line, and answers from the per-call
# scripts the case queued: FAKE_<KIND>_ANSWERS holds one line per expected
# call of that kind, each `<exit>|<stderr>|<stdout>`; a kind with no more
# lines answers the last one again.
printf '%s\n' "$(printf '%s\n' "$@" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().split("\n")[:-1]))')" >> "${FAKE_GCLOUD_LOG}"
answer() {
  local kind counter n
  kind="$1"
  counter="${FAKE_GCLOUD_LOG}.${kind}.n"
  n=0
  [ -f "$counter" ] && n="$(cat "$counter")"
  echo $((n + 1)) > "$counter"
  local var lines
  var="FAKE_${kind}_ANSWERS"
  lines="${!var:-}"
  local line
  line="$(printf '%s\n' "$lines" | sed -n "$((n + 1))p")"
  [ -n "$line" ] || line="$(printf '%s\n' "$lines" | tail -n 1)"
  # One assignment per line: bash expands every word of a `local` statement
  # before it assigns any, so a later word cannot read an earlier one.
  local rc rest err out
  rc="${line%%|*}"
  rest="${line#*|}"
  err="${rest%%|*}"
  out="${rest#*|}"
  [ -n "$err" ] && printf '%s\n' "$err" >&2
  [ -n "$out" ] && printf '%s\n' "$out"
  exit "${rc:-0}"
}
case "$1 $2 $3" in
  "iam service-accounts describe") answer SA_DESCRIBE ;;
  "iam service-accounts create") answer SA_CREATE ;;
  "projects add-iam-policy-binding "*) answer BIND ;;
  "container operations list") answer OPS ;;
  "container clusters update") answer UPDATE ;;
esac
echo "fake gcloud: unexpected call: $*" >&2
exit 97
"""


def _constants() -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    found = _CONSTANT_RE.findall(text)
    assert len(found) == 10, f"expected the ten lifted constants, found {found}"
    assert _HOST_NAME_RE.search(text), "HOST_CLUSTER_NAME is no longer a plain assignment"
    return "\n".join(found) + '\nHOST_CLUSTER_NAME="platform-agent-host"\nreadonly FORGE_VM_BIND_DELAY_S=0\nreadonly HOST_OTEL_WAIT_DELAY_S=0\n'


def _section(start: str, end: str) -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    i = text.find(start)
    assert i != -1, f"{start!r} not found in {_SCRIPT}"
    j = text.find(end, i)
    assert j != -1, f"{end!r} does not follow {start!r}"
    j = text.index("\n", j) + 1
    return text[i:j]


class _RetryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bindir = pathlib.Path(self.tmp.name) / "bin"
        bindir.mkdir()
        fake = bindir / "gcloud"
        fake.write_text(_FAKE_GCLOUD)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.bindir = bindir
        self.log = pathlib.Path(self.tmp.name) / "gcloud.log"
        self.errfile = pathlib.Path(self.tmp.name) / "gcloud.err"

    def _run(self, body: str, answers: dict):
        env = get_isolated_test_env(
            overrides={
                "PATH": f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                "FAKE_GCLOUD_LOG": str(self.log),
                "PROJECT_ID": _PROJECT,
                "REGION": _REGION,
                **{f"FAKE_{kind}_ANSWERS": "\n".join(lines) for kind, lines in answers.items()},
            }
        )
        script = "set -euo pipefail\n" + _constants() + f'GCLOUD_ERR_FILE="{self.errfile}"\n' + body
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return proc, calls


class ForgeVmBindingRetryTest(_RetryCase):
    """Step 1.5: the binding on the account the step just created."""

    def _bind(self, bind_answers):
        return self._run(
            _section(_FORGE_START, _FORGE_END),
            {"SA_DESCRIBE": ["1|not found|"], "SA_CREATE": ["0||"], "BIND": bind_answers},
        )

    def _bind_calls(self, calls):
        return [c for c in calls if c[:2] == ["projects", "add-iam-policy-binding"]]

    def test_a_binding_refused_as_not_existing_is_retried_until_it_takes(self):
        refused = f"1|ERROR: (gcloud.projects.add-iam-policy-binding) INVALID_ARGUMENT: Service account forge-vm@{_PROJECT}.iam.gserviceaccount.com does not exist.|"
        proc, calls = self._bind([refused, refused, "0||Updated IAM policy"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._bind_calls(calls)), 3)
        self.assertIn("not visible to IAM yet (attempt 2/", proc.stdout)
        self.assertIn("✓ Forge pieces", proc.stdout)

    def test_any_other_refusal_stops_the_step_at_once_and_says_so(self):
        denied = "1|ERROR: (gcloud.projects.add-iam-policy-binding) PERMISSION_DENIED: caller lacks resourcemanager.projects.setIamPolicy|"
        proc, calls = self._bind([denied])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self._bind_calls(calls)), 1)
        self.assertIn("PERMISSION_DENIED", proc.stderr)
        self.assertIn("failed (above)", proc.stderr)
        self.assertNotIn("still not visible", proc.stderr)

    def test_an_account_that_never_appears_gives_up_after_the_budget(self):
        refused = "1|ERROR: Service account does not exist.|"
        proc, calls = self._bind([refused])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self._bind_calls(calls)), _FORGE_ATTEMPTS)
        self.assertIn(f"still not visible to IAM after {_FORGE_ATTEMPTS} attempts", proc.stderr)


class OtelScopeOperationWaitTest(_RetryCase):
    """Step 2.1: the scope update behind the cluster's running operations."""

    def _scope(self, ops_answers, update_answers):
        return self._run(_section(_OTEL_START, _OTEL_END), {"OPS": ops_answers, "UPDATE": update_answers})

    @staticmethod
    def _calls(calls, *prefix):
        return [c for c in calls if c[: len(prefix)] == list(prefix)]

    def test_the_update_waits_for_running_operations_then_runs_once(self):
        running = "0||operation-1 CREATE_NODE_POOL"
        proc, calls = self._scope([running, running, "0||"], ["0||"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._calls(calls, "container", "operations", "list")), 3)
        self.assertEqual(len(self._calls(calls, "container", "clusters", "update")), 1)
        self.assertIn("is running an operation (operation-1 CREATE_NODE_POOL)", proc.stdout)
        self.assertIn("✓ Managed OpenTelemetry scope", proc.stdout)

    def test_the_poll_filters_on_the_cluster_and_its_node_pools_with_a_quoted_regex(self):
        # gcloud's filter lexer ends an unquoted operand at "(": the regex has
        # to be quoted inside the filter or the poll matches nothing.
        proc, calls = self._scope(["0||"], ["0||"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (poll,) = self._calls(calls, "container", "operations", "list")
        filters = [a for a in poll if a.startswith("--filter=")]
        self.assertEqual(len(filters), 1, poll)
        self.assertRegex(filters[0], _QUOTED_FILTER_RE)
        self.assertIn("status!=DONE", filters[0])
        self.assertIn(f"--project={_PROJECT}", poll)
        self.assertIn(f"--location={_REGION}", poll)

    def test_an_update_refused_for_a_running_operation_is_retried(self):
        busy = "1|ERROR: (gcloud.container.clusters.update) FAILED_PRECONDITION: Cluster is running incompatible operation operation-2. reason: CLUSTER_ALREADY_HAS_OPERATION|"
        proc, calls = self._scope(["0||"], [busy, "0||"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._calls(calls, "container", "clusters", "update")), 2)
        self.assertIn("an operation started between the poll and the update", proc.stdout)

    def test_any_other_update_failure_stops_the_step(self):
        denied = "1|ERROR: (gcloud.container.clusters.update) PERMISSION_DENIED: container.clusters.update|"
        proc, calls = self._scope(["0||"], [denied])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self._calls(calls, "container", "clusters", "update")), 1)
        self.assertIn("PERMISSION_DENIED", proc.stderr)
        self.assertIn("could not set the managed OpenTelemetry scope", proc.stderr)

    def test_a_cluster_that_never_frees_gives_up_after_the_budget_and_names_the_resume(self):
        running = "0||operation-3 CREATE_NODE_POOL"
        proc, calls = self._scope([running], ["0||"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self._calls(calls, "container", "operations", "list")), _OTEL_ATTEMPTS)
        self.assertEqual(len(self._calls(calls, "container", "clusters", "update")), 0)
        self.assertIn("--skip-host-cluster", proc.stderr)

    def test_a_failed_operations_list_falls_through_to_the_update(self):
        proc, calls = self._scope(["1|ERROR: (gcloud.container.operations.list) PERMISSION_DENIED|"], ["0||"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._calls(calls, "container", "clusters", "update")), 1)


class ScriptShapeTest(unittest.TestCase):
    def test_the_lifted_sections_are_where_the_markers_say(self):
        forge = _section(_FORGE_START, _FORGE_END)
        otel = _section(_OTEL_START, _OTEL_END)
        self.assertIn("FORGE_VM_BIND_RETRY_PATTERN", forge)
        self.assertIn("HOST_OTEL_BUSY_PATTERN", otel)
        self.assertNotIn("/tmp/", forge + otel)


if __name__ == "__main__":
    unittest.main()
