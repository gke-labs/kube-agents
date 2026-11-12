"""Leftover NATS JetStream claims are deleted by name, in teardown and before deploy (#5658).

The NATS StatefulSet that mode: next renders takes its claims from
volumeClaimTemplates, which carry no labels, so the teardown's part-of sweep
never reaches them. A claim left behind pins its server to the old disk's zone,
and the next install's topology spread can leave that server unschedulable,
failing the deploy before any case runs.

Two fixes, two tests:

* ``hack/ci-teardown.sh`` Step 6 deletes ``data-platform-agent-a2a-nats-<n>``
  claims after the namespaced sweep, and no other claim; a failing kubectl is
  counted, never fatal.
* ``hack/ci-deploy.sh``'s ``delete_orphaned_nats_claims`` deletes them before
  the chart install only when no ``platform-agent-a2a-nats`` StatefulSet
  exists, and never stops the deploy.

Both run the script's own text under bash against a kubectl stub on PATH, the
approach of tests/test_ci_teardown_sweep.py.
"""

import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_TEARDOWN = _REPO_ROOT / "hack" / "ci-teardown.sh"
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_NAMESPACE = "kubeagents-system"
_NATS_CLAIMS = (
    "persistentvolumeclaim/data-platform-agent-a2a-nats-0",
    "persistentvolumeclaim/data-platform-agent-a2a-nats-1",
    "persistentvolumeclaim/data-platform-agent-a2a-nats-2",
)
# Claims a prefix-only or broader match would wrongly take.
_OTHER_CLAIMS = (
    "persistentvolumeclaim/data-platform-agent-shell-0",
    "persistentvolumeclaim/platform-agent-data",
    "persistentvolumeclaim/data-platform-agent-a2a-nats-backup",
    "persistentvolumeclaim/hindsight-data-0",
)
_STATEFULSET = "statefulset.apps/platform-agent-a2a-nats"

_TEARDOWN_STEPS_START = "START_TIME=$SECONDS"
_DEPLOY_FUNCTION_RE = re.compile(r"^delete_orphaned_nats_claims\(\) \{\n.*?^\}\n", re.MULTILINE | re.DOTALL)
_DEPLOY_CONSTANTS = (
    "PLATFORM_AGENT_CR_NAME",
    "A2A_NATS_STATEFULSET_NAME",
    "A2A_NATS_CLAIM_NAME_RE",
    "A2A_NATS_CLAIM_DELETE_TIMEOUT",
)

# The stub logs its argv, answers the two `get`s from the environment, and
# exits STUB_GET_EXIT on a `get` and STUB_EXIT on everything else.
_KUBECTL_STUB = """#!/usr/bin/env bash
echo "kubectl $*" >> "$STUB_LOG"
if [ "$1" = "get" ]; then
  if [ -n "${STUB_GET_EXIT:-}" ] && [ "$STUB_GET_EXIT" != "0" ]; then
    echo "error: the server is currently unable to handle the request" >&2
    exit "$STUB_GET_EXIT"
  fi
  case "$2" in
    persistentvolumeclaims) printf '%s' "$STUB_PVCS" ;;
    statefulset) printf '%s' "$STUB_STS" ;;
  esac
  exit 0
fi
exit "${STUB_EXIT:-0}"
"""


def _run(script, pvcs, sts="", kubectl_exit=0, get_exit=0, extra_env=None):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        log.touch()
        for tool, body in (
            ("kubectl", _KUBECTL_STUB),
            ("helm", "#!/usr/bin/env bash\nexit 0\n"),
            ("bench-sweep", "#!/usr/bin/env bash\nexit 0\n"),
        ):
            stub = bin_dir / tool
            stub.write_text(body, encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = {
            "NAMESPACE": _NAMESPACE,
            "STUB_LOG": str(log),
            "STUB_PVCS": "\n".join(pvcs) + ("\n" if pvcs else ""),
            "STUB_STS": sts,
            "STUB_EXIT": str(kubectl_exit),
            "STUB_GET_EXIT": str(get_exit),
            "CI_TEARDOWN_BENCH_SWEEP_SCRIPT": str(bin_dir / "bench-sweep"),
            "PROJECT_ID": "kube-agents-evals-7",
            "CLUSTER_NAME": "platform-agent-host",
            "REGION": "us-east4",
        }
        env.update(extra_env or {})
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            cwd=_REPO_ROOT,
            env=get_isolated_test_env(bin_dir=bin_dir, overrides=env),
        )
        calls = log.read_text(encoding="utf-8").splitlines()
    return proc, calls


def _claim_deletes(calls):
    return [c for c in calls if c.startswith("kubectl delete") and "persistentvolumeclaim/" in c]


def _teardown_script():
    text = _CI_TEARDOWN.read_text(encoding="utf-8")
    start = text.find(_TEARDOWN_STEPS_START)
    assert start != -1, f"{_TEARDOWN_STEPS_START!r} not found in hack/ci-teardown.sh"
    constants = "\n".join(line for line in text[:start].splitlines() if line.startswith("readonly "))
    return "set -uo pipefail\n" + constants + "\n" + text[start:]


def _deploy_script():
    text = _CI_DEPLOY.read_text(encoding="utf-8")
    lines = []
    for name in _DEPLOY_CONSTANTS:
        match = re.search(rf"^readonly {name}=.*$", text, re.MULTILINE)
        assert match, f"hack/ci-deploy.sh no longer declares readonly {name}"
        lines.append(match.group(0))
    function = _DEPLOY_FUNCTION_RE.search(text)
    assert function, "hack/ci-deploy.sh no longer defines delete_orphaned_nats_claims"
    return "set -euo pipefail\n" + "\n".join(lines) + "\n" + function.group(0) + "delete_orphaned_nats_claims\necho REACHED_END\n"


class TeardownNatsClaimsTest(unittest.TestCase):
    def test_deletes_every_nats_claim_by_name_and_nothing_else(self):
        proc, calls = _run(_teardown_script(), _NATS_CLAIMS + _OTHER_CLAIMS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deletes = _claim_deletes(calls)
        for claim in _NATS_CLAIMS:
            with self.subTest(claim=claim):
                matching = [c for c in deletes if claim in c.split()]
                self.assertEqual(len(matching), 1, deletes)
                argv = matching[0].split()
                self.assertIn("--ignore-not-found", argv)
                self.assertIn(_NAMESPACE, argv)
                self.assertNotIn("-l", argv)
                # Its pod may still hold pvc-protection; waiting would count a
                # healthy teardown as failed.
                self.assertIn("--wait=false", argv)
        for claim in _OTHER_CLAIMS:
            with self.subTest(claim=claim):
                self.assertFalse([c for c in deletes if claim in c.split()], deletes)

    def test_claims_are_deleted_after_the_statefulset_sweep(self):
        proc, calls = _run(_teardown_script(), _NATS_CLAIMS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sts_sweep = next(i for i, c in enumerate(calls) if c.startswith("kubectl delete statefulsets.apps"))
        first_claim = next(i for i, c in enumerate(calls) if c in _claim_deletes(calls))
        self.assertLess(sts_sweep, first_claim, calls)

    def test_failure_is_counted_and_never_fatal(self):
        for label, kwargs in (("delete", {"kubectl_exit": 1}), ("list", {"get_exit": 1})):
            with self.subTest(failing=label):
                proc, _ = _run(_teardown_script(), _NATS_CLAIMS, **kwargs)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("✗ NATS claim delete", proc.stdout)

    def test_strict_mode_reds_on_a_failed_delete(self):
        proc, _ = _run(_teardown_script(), _NATS_CLAIMS, kubectl_exit=1, extra_env={"CI_TEARDOWN_STRICT": "1"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("NATS claim delete", proc.stdout)

    def test_no_claims_is_a_clean_step(self):
        proc, calls = _run(_teardown_script(), _OTHER_CLAIMS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("✓ NATS claim delete (0 failed of 0 found)", proc.stdout)
        self.assertFalse([c for c in _claim_deletes(calls) if "a2a-nats" in c], calls)


class DeployOrphanedNatsClaimsTest(unittest.TestCase):
    def test_deletes_leftover_claims_when_no_statefulset_exists(self):
        proc, calls = _run(_deploy_script(), _NATS_CLAIMS + _OTHER_CLAIMS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REACHED_END", proc.stdout)
        deletes = _claim_deletes(calls)
        deleted = sorted(a for c in deletes for a in c.split() if a.startswith("persistentvolumeclaim/"))
        self.assertEqual(deleted, sorted(_NATS_CLAIMS))
        for call in deletes:
            self.assertIn("--ignore-not-found", call.split())
            self.assertIn(_NAMESPACE, call.split())

    def test_leaves_claims_a_live_statefulset_owns(self):
        proc, calls = _run(_deploy_script(), _NATS_CLAIMS, sts=_STATEFULSET + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REACHED_END", proc.stdout)
        self.assertEqual(_claim_deletes(calls), [])
        self.assertFalse([c for c in calls if c.startswith("kubectl get persistentvolumeclaims")], calls)

    def test_an_unreadable_statefulset_leaves_claims_and_continues(self):
        proc, calls = _run(_deploy_script(), _NATS_CLAIMS, get_exit=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REACHED_END", proc.stdout)
        self.assertEqual(_claim_deletes(calls), [])

    def test_a_failed_delete_never_stops_the_deploy(self):
        proc, _ = _run(_deploy_script(), _NATS_CLAIMS, kubectl_exit=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REACHED_END", proc.stdout)
        self.assertIn("could not delete", proc.stdout)

    def test_the_call_precedes_the_chart_install(self):
        text = _CI_DEPLOY.read_text(encoding="utf-8")
        call = re.search(r"^delete_orphaned_nats_claims$", text, re.MULTILINE)
        install = text.find('helm upgrade --install "${HELM_RELEASE_NAME}"')
        self.assertIsNotNone(call, "delete_orphaned_nats_claims is never called")
        self.assertNotEqual(install, -1)
        self.assertLess(call.start(), install)


if __name__ == "__main__":
    unittest.main()
