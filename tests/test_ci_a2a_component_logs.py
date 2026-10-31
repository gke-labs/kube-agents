"""The lane keeps the logs of every A2A component pod, previous instance included.

A `spec.mode: next` deploy that fails because an A2A pod crash-loops used to
leave `describe` output and nothing else: "exit code 1", with the reason in a
container log no step collected (ci-kube-agents-eval-next build
2108361692286029824, the auth callout). hack/ci-env.sh's
collect_agent_pod_diagnostics, which both the eval's exit trap and the failure
dumper call, now reads every pod carrying the operator's A2A component label,
and every session pod the gateway spawned, and writes
a2a-<component>-<pod>.log plus a2a-<component>-<pod>-previous.log for one
with a restarted container.

ci-env.sh is sourced whole and the real collector runs, with `kubectl` on PATH
replaced by a stub that answers the pod listings from fixtures and the log
reads with text naming the pod and whether it was the previous instance.
"""

import pathlib
import stat
import subprocess
import tempfile
import textwrap
import unittest

from tests._lift_shell import lift_constant
from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_ENV = _REPO_ROOT / "hack" / "ci-env.sh"

_NS = "kubeagents-system"
_CONTEXT = "gke_p_r_host"
_COMPONENT_SELECTOR = "kubeagents.x-k8s.io/a2a-component"
_SESSION_SELECTOR = "app.kubernetes.io/component=a2a-session"

_CRASHING = "platform-agent-a2a-callout-75b57d6cc9-ckj2k"
_READY = "platform-agent-a2a-callout-75b57d6cc9-qhr2l"
_NATS = "platform-agent-a2a-nats-0"
_SESSION = "a2a-session-7f3k2"

# The listings, in the shape A2A_DIAG_PODS_JSONPATH prints: name, component
# label, each container's restart count. The session pod has no component
# label and two containers, the second restarted.
_COMPONENT_LISTING = f"{_CRASHING}\tcallout\t6\n{_READY}\tcallout\t0\n{_NATS}\tnats\t0\n"
_SESSION_LISTING = f"{_SESSION}\t\t0 2\n"

_KUBECTL_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Every call is logged, one line, so a test can pin the flags a read used.
    echo "$*" >> "${KUBECTL_CALL_LOG}"
    args=" $* "
    case "${args}" in
      *" get pods "*)
        if [ -n "${KUBECTL_LIST_FAILS:-}" ]; then
          echo "Unable to connect to the server: dial tcp: i/o timeout" >&2
          exit 1
        fi
        case "${args}" in
          *" -l kubeagents.x-k8s.io/a2a-component "*) cat "${KUBECTL_COMPONENT_LISTING}" ;;
          *" -l app.kubernetes.io/component=a2a-session "*) cat "${KUBECTL_SESSION_LISTING}" ;;
          # The agent diagnostics' own namespace-wide pod status read.
          *) : ;;
        esac
        ;;
      *" logs pod/"*)
        pod="${args#* logs pod/}"
        pod="${pod%% *}"
        case "${args}" in
          *" --previous "*) echo "PREVIOUS ${pod}: auth callout exiting" ;;
          *) echo "CURRENT ${pod}" ;;
        esac
        ;;
      *) : ;;
    esac
    exit 0
    """
)


class A2AComponentLogsTest(unittest.TestCase):
    def collect(self, *, component_listing=_COMPONENT_LISTING, session_listing=_SESSION_LISTING, list_fails=False, prefix=""):
        """Runs collect_agent_pod_diagnostics under `set -euo pipefail`, as the
        eval's trap does. Returns the result, the artifacts dir and the calls."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(_KUBECTL_STUB)
        kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
        artifacts = root / "artifacts"
        calls = root / "calls.log"
        calls.touch()
        (root / "component.txt").write_text(component_listing)
        (root / "session.txt").write_text(session_listing)
        env = get_isolated_test_env(
            overrides={
                "ARTIFACTS": str(artifacts),
                "KUBECTL_CALL_LOG": str(calls),
                "KUBECTL_COMPONENT_LISTING": str(root / "component.txt"),
                "KUBECTL_SESSION_LISTING": str(root / "session.txt"),
                "AGENT_CLUSTER_CONTEXT": _CONTEXT,
                "KUBECTL_LIST_FAILS": "1" if list_fails else "",
            },
            bin_dir=str(bin_dir),
            absent=("TARGET_NAMESPACE", "NAMESPACE"),
        )
        script = textwrap.dedent(
            f"""\
            set -euo pipefail
            source {_CI_ENV}
            AGENT_DIAG_PREFIX={prefix!r}
            collect_agent_pod_diagnostics
            echo "collector returned=$?"
            """
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False, env=env)
        return result, artifacts, calls.read_text().splitlines()

    def logs_calls(self, calls, pod):
        return [c for c in calls if f" logs pod/{pod} " in f" {c} "]

    def test_a_crash_looping_callout_gets_its_current_and_previous_log(self) -> None:
        result, artifacts, calls = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("collector returned=0", result.stdout)
        self.assertEqual((artifacts / f"a2a-callout-{_CRASHING}.log").read_text(), f"CURRENT {_CRASHING}\n")
        self.assertEqual(
            (artifacts / f"a2a-callout-{_CRASHING}-previous.log").read_text(),
            f"PREVIOUS {_CRASHING}: auth callout exiting\n",
        )
        reads = self.logs_calls(calls, _CRASHING)
        self.assertEqual(len(reads), 2, reads)
        for read in reads:
            self.assertIn(f"--context {_CONTEXT}", read)
            self.assertIn(f"-n {_NS}", read)
            self.assertIn("--all-containers", read)
            self.assertIn("--ignore-errors", read)
            self.assertIn("--prefix", read)
            self.assertIn("--tail=", read)
        self.assertEqual(sum("--previous" in r for r in reads), 1, reads)

    def test_a_pod_that_never_restarted_gets_no_previous_read(self) -> None:
        result, artifacts, calls = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        for component, pod in (("callout", _READY), ("nats", _NATS)):
            with self.subTest(pod=pod):
                self.assertEqual((artifacts / f"a2a-{component}-{pod}.log").read_text(), f"CURRENT {pod}\n")
                self.assertFalse((artifacts / f"a2a-{component}-{pod}-previous.log").exists())
                self.assertFalse(any("--previous" in c for c in self.logs_calls(calls, pod)))

    def test_a_session_pod_is_named_worker_and_any_restarted_container_counts(self) -> None:
        result, artifacts, calls = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((artifacts / f"a2a-worker-{_SESSION}.log").exists())
        self.assertTrue((artifacts / f"a2a-worker-{_SESSION}-previous.log").exists())
        selectors = [c for c in calls if " get pods " in f" {c} " and " -l " in f" {c} "]
        self.assertTrue(any(f"-l {_COMPONENT_SELECTOR} " in f"{c} " for c in selectors), selectors)
        self.assertTrue(any(f"-l {_SESSION_SELECTOR} " in f"{c} " for c in selectors), selectors)

    def test_the_rollback_prefix_reaches_the_file_names(self) -> None:
        result, artifacts, _ = self.collect(prefix="rollback-")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((artifacts / f"rollback-a2a-callout-{_CRASHING}-previous.log").exists())
        self.assertTrue((artifacts / "rollback-a2a-component-pods.txt").exists())
        self.assertFalse((artifacts / f"a2a-callout-{_CRASHING}.log").exists())

    def test_a_today_mode_install_gains_no_log_files(self) -> None:
        result, artifacts, calls = self.collect(component_listing="", session_listing="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in artifacts.glob("a2a-*.log")), [])
        self.assertEqual([c for c in calls if " logs pod/" in f" {c}"], [])

    def test_a_failed_listing_is_on_record_and_does_not_fail_the_step(self) -> None:
        result, artifacts, calls = self.collect(list_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("collector returned=0", result.stdout)
        self.assertIn("Unable to connect to the server", (artifacts / "a2a-component-pods.txt").read_text())
        self.assertEqual(sorted(p.name for p in artifacts.glob("a2a-*.log")), [])
        self.assertEqual([c for c in calls if " logs pod/" in f" {c}"], [])

    def test_the_pod_count_is_bounded(self) -> None:
        many = "".join(f"platform-agent-a2a-x-{i:03d}\tverifier\t1\n" for i in range(60))
        result, artifacts, _ = self.collect(component_listing=many, session_listing="")
        self.assertEqual(result.returncode, 0, result.stderr)
        current = [p for p in artifacts.glob("a2a-verifier-*.log") if not p.name.endswith("-previous.log")]
        bound = int(lift_constant("A2A_DIAG_MAX_PODS", _CI_ENV.read_text(), _CI_ENV).split("=", 1)[1])
        self.assertLess(bound, 60)
        self.assertEqual(len(current), bound)
        self.assertIn(f"truncated: read the first {bound} pods", (artifacts / "a2a-component-pods.txt").read_text())


if __name__ == "__main__":
    unittest.main()
