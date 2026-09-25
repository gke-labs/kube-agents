"""The in-flight note release: a repetition that died mid-run does not refuse the next.

`audit_report.py start` leaves `/opt/data/scratch/inflight_<audit>.json` on the
sandbox pod and refuses while one younger than two hours exists; `finish`
removes it. A repetition whose worker died between the two leaves the note,
and the next repetition of the case (and, on a stream two cases share, the
sibling case's first) starts inside those two hours and is refused.
`hack/ci-eval-pr.sh` therefore releases that unit's stream's note before each
audit unit, from the same place the ledger reset runs. What has to hold,
checked against the shell that ships (lifted out of the script) with
`kubectl` and `timeout` stubbed:

  - it reaches one pod only: the leased project's host-cluster context
    (AGENT_CLUSTER_CONTEXT, which names PROJECT_ID), the lease's namespace, the
    operator's `<agent>-shell` StatefulSet's one replica, container `shell`;
  - it removes one path only: the stream's note under /opt/data/scratch, the
    audit id a bare label, handed to the pod's shell as data rather than
    spliced into a command line; the `.lock` beside the note stays;
  - a note that is there is given the grace period to be released by its own
    run first, since a unit that ended on its ceiling may have left a live
    worker, and is removed only if it outlives that;
  - the exec is bounded outside kubectl, so a hung stream cannot hold the
    task and stream locks, and the lock deadline grants a unit its grace;
  - a release that cannot run says why and the unit goes on: no context, a
    context that is another project's, no kubectl, an exec that fails or
    times out;
  - it sits with the ledger reset: after it, before devops-bench, inside the
    task and stream locks, gated on the case writing a ledger.
"""

import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
DEPLOY = REPO_ROOT / "hack" / "ci-deploy.sh"
OPERATOR_MANIFESTS = REPO_ROOT / "k8s-operator" / "internal" / "controller" / "shell_sandbox_manifests.go"
AUDIT_REPORT = REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "audit_report.py"

PROJECT = "kube-agents-evals-2"
CONTEXT = f"gke_{PROJECT}_us-central1_platform-agent-host"
NAMESPACE = "kubeagents-system"
POD = "platform-agent-shell-0"
NOTE = "/opt/data/scratch/inflight_compliance-audit.json"

CONSTANT_LINES = [
    r"^readonly EVAL_SANDBOX_CONTAINER=.*$",
    r"^readonly EVAL_SANDBOX_SCRATCH_DIR=.*$",
    r"^readonly EVAL_SANDBOX_EXEC_TIMEOUT=.*$",
    r"^readonly EVAL_SANDBOX_EXEC_ROUND_TRIP_SECONDS=.*$",
    r"^readonly EVAL_INFLIGHT_GRACE_SECONDS=.*$",
]


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


def grace_seconds() -> int:
    return int(lifted_line(CONSTANT_LINES[4]).split("=", 1)[1])


def round_trip_seconds() -> int:
    return int(lifted_line(CONSTANT_LINES[3]).split("=", 1)[1])


def pod_snippet() -> str:
    """The script the pod's shell runs, as shipped."""
    match = re.search(r"sh -c '(.*?)' sh \"\$\{note\}\"", lifted("release_inflight_note"), re.DOTALL)
    assert match, "the pod snippet is no longer a single-quoted sh -c body"
    return match.group(1)


def run_bash(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -uo pipefail\n" + body],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


# Records its argv NUL-separated (the pod snippet spans lines) and answers as
# told; kubectl itself is never run.
STUB_KUBECTL = textwrap.dedent(
    """\
    #!/bin/bash
    printf '%s\\0' "$@" > "${KUBECTL_ARGV_FILE}"
    printf '%s' "${KUBECTL_OUT:-}"
    exit "${KUBECTL_RC:-0}"
    """
)

# Records the bound it was asked for, then runs the command (or plays the
# real timeout's 124 when told to); shadows the system one on every platform.
STUB_TIMEOUT = textwrap.dedent(
    """\
    #!/bin/bash
    printf '%s\\n' "$1" "$2" > "${TIMEOUT_ARGV_FILE}"
    shift 2
    if [ -n "${TIMEOUT_FIRES:-}" ]; then exit 124; fi
    exec "$@"
    """
)


class ReleaseStepTest(unittest.TestCase):
    """release_inflight_note with kubectl and timeout stubbed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        (self.dir / "empty").mkdir()
        for name, text in [("kubectl", STUB_KUBECTL), ("timeout", STUB_TIMEOUT)]:
            stub = self.bin / name
            stub.write_text(text)
            stub.chmod(0o755)
        self.argv_file = self.dir / "argv"
        self.timeout_file = self.dir / "timeout-argv"

    def run_step(
        self,
        label="compliance-rbac-overgrant rep 2",
        audit_id="compliance-audit",
        project=PROJECT,
        context=CONTEXT,
        namespace=NAMESPACE,
        kubectl=True,
        out=f"none at {NOTE}",
        rc=0,
        timeout_fires=False,
        extra="",
    ):
        path = f"{self.bin}:{os.environ.get('PATH', '')}" if kubectl else str(self.dir / "empty")
        body = "\n".join(
            [
                f'export PATH="{path}"',
                f'PROJECT_ID="{project}"; AGENT_CLUSTER_CONTEXT="{context}"; TARGET_NAMESPACE="{namespace}"',
                'AGENT_SERVICE_NAME="platform-agent"',
                extra,
                *(lifted_line(pattern) for pattern in CONSTANT_LINES),
                lifted("release_inflight_note"),
                f"release_inflight_note {shlex.quote(label)} {shlex.quote(audit_id)}",
                'echo "RC=$?"',
            ]
        )
        return run_bash(
            body,
            {
                "KUBECTL_ARGV_FILE": str(self.argv_file),
                "KUBECTL_OUT": out,
                "KUBECTL_RC": str(rc),
                "TIMEOUT_ARGV_FILE": str(self.timeout_file),
                "TIMEOUT_FIRES": "1" if timeout_fires else "",
            },
        )

    def argv(self):
        return self.argv_file.read_text().split("\0")[:-1]

    def test_the_unit_release_reaches_the_lease_pod_and_the_streams_note_only(self):
        result = self.run_step()
        self.assertIn("RC=0", result.stdout, result.stderr)
        argv = self.argv()
        self.assertEqual(
            argv[:12],
            ["--context", CONTEXT, "-n", NAMESPACE, "exec", POD, "-c", "shell", "--request-timeout=30s", "--", "sh", "-c"],
        )
        # The path and the grace are the shell's positionals, never part of
        # the -c script.
        self.assertEqual(argv[13:], ["sh", NOTE, str(grace_seconds())])
        self.assertIn('"$1"', argv[12])
        self.assertNotIn("compliance-audit", argv[12])
        self.assertNotIn(".lock", " ".join(argv))
        self.assertIn(f"In-flight note (compliance-rbac-overgrant rep 2): none at {NOTE}", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_the_exec_is_bounded_by_the_grace_plus_one_round_trip(self):
        # Outside kubectl, which cannot bound its own exec stream.
        self.run_step()
        self.assertEqual(self.timeout_file.read_text().splitlines(), ["--foreground", str(grace_seconds() + round_trip_seconds())])
        # And the bound sits outside kubectl: --request-timeout covers the
        # upgrade round trip only, not the exec stream.
        function = lifted("release_inflight_note")
        self.assertIn('bound=(timeout --foreground "${budget}")', function)
        self.assertIn("command -v timeout >/dev/null 2>&1 || bound=()", function)

    def test_a_timed_out_exec_warns_and_the_unit_goes_on(self):
        result = self.run_step(timeout_fires=True)
        self.assertIn(
            f"WARNING: In-flight note (compliance-rbac-overgrant rep 2): kubectl exec into {NAMESPACE}/{POD} exited 124 (timed out after {grace_seconds() + round_trip_seconds()}s; the loop left in the pod may still remove the note)",
            result.stderr,
        )
        self.assertFalse(self.argv_file.exists())
        self.assertIn("RC=0", result.stdout)

    def test_the_pods_shell_snippet_removes_a_note_only_after_its_grace(self):
        # The snippet as shipped, run by a local sh against real files: with
        # no grace a note is removed at once; with a grace, a note its own run
        # releases in time is left to it; one that outlives the grace goes.
        # The lock beside the note is never touched.
        snippet = pod_snippet()
        lock = self.dir / "inflight_compliance-audit.json.lock"
        lock.touch()

        def run(note, grace):
            return subprocess.run(["sh", "-c", snippet, "sh", str(note), str(grace)], capture_output=True, text=True, check=False)

        note = self.dir / "inflight_compliance-audit.json"
        note.write_text('{"audit": "compliance-audit"}')
        result = run(note, 0)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"removed {note} after waiting 0s for its run"), result.stderr)
        self.assertFalse(note.exists())
        result = run(note, 0)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"none at {note}"), result.stderr)

        note.write_text('{"audit": "compliance-audit"}')
        releaser = threading.Timer(1.0, note.unlink)
        releaser.start()
        self.addCleanup(releaser.cancel)
        result = run(note, 5)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"released by its own run after 5s: {note}"), result.stderr)

        note.write_text('{"audit": "compliance-audit"}')
        started = time.monotonic()
        result = run(note, 5)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"removed {note} after waiting 5s for its run"), result.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 5.0)
        self.assertFalse(note.exists())
        self.assertTrue(lock.exists())

    def test_the_pod_can_be_named_and_defaults_to_the_agents_shell(self):
        result = self.run_step(extra='EVAL_SANDBOX_POD="other-shell-0"')
        self.assertEqual(self.argv()[5], "other-shell-0")
        self.assertIn("RC=0", result.stdout)
        # AGENT_SERVICE_NAME is what the script exports for the agent; the
        # default pod is that name's StatefulSet, replica 0.
        self.assertIn('pod="${EVAL_SANDBOX_POD:-${AGENT_SERVICE_NAME}-shell-0}"', lifted("release_inflight_note"))

    def test_a_context_that_is_not_the_projects_is_refused_before_any_call(self):
        for context in [
            "gke_kube-agents-evals-3_us-central1_platform-agent-host",
            # The project's name as a prefix of another's: the underscore after
            # PROJECT_ID is part of the match.
            "gke_kube-agents-evals-22_us-central1_platform-agent-host",
            "kube-agents-evals-2",
            "gke_other_us-central1_kube-agents-evals-2",
        ]:
            with self.subTest(context=context):
                result = self.run_step(label="lease-x", context=context)
                self.assertIn(
                    f"WARNING: In-flight note (lease-x): skipped, AGENT_CLUSTER_CONTEXT={context} does not name PROJECT_ID={PROJECT}",
                    result.stderr,
                )
                self.assertFalse(self.argv_file.exists())
                self.assertIn("RC=0", result.stdout)

    def test_an_unset_project_context_or_namespace_skips_out_loud(self):
        for field in ["project", "context", "namespace"]:
            with self.subTest(field=field):
                result = self.run_step(**{field: ""})
                self.assertIn("skipped, PROJECT_ID, AGENT_CLUSTER_CONTEXT or TARGET_NAMESPACE is unset", result.stdout)
                self.assertIn("the compliance-audit stream keeps whatever note is on the sandbox", result.stdout)
                self.assertFalse(self.argv_file.exists())
                self.assertIn("RC=0", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_an_audit_id_that_is_not_a_bare_label_is_refused_without_a_call(self):
        for audit_id in ["", "../etc", "a b", "x;y", "$(x)", "compliance audit", "a/b", "*"]:
            with self.subTest(audit_id=audit_id):
                result = self.run_step(audit_id=audit_id)
                self.assertIn(f"skipped, audit id '{audit_id}' is not a bare label", result.stdout)
                self.assertFalse(self.argv_file.exists())
                self.assertIn("RC=0", result.stdout)
        # The ids the cases carry are bare labels.
        for audit_id in ["compliance-audit", "fleet-consistency-drift", "security-patch-orchestrator", "ai-security-audit"]:
            with self.subTest(audit_id=audit_id):
                self.run_step(audit_id=audit_id)
                self.assertEqual(self.argv()[-2], f"/opt/data/scratch/inflight_{audit_id}.json")

    def test_no_kubectl_on_path_skips_out_loud(self):
        result = self.run_step(kubectl=False)
        self.assertIn("In-flight note (compliance-rbac-overgrant rep 2): skipped, no kubectl on PATH", result.stdout)
        self.assertFalse(self.argv_file.exists())
        self.assertIn("RC=0", result.stdout)

    def test_a_failing_exec_warns_and_the_unit_goes_on(self):
        result = self.run_step(out="error: unable to upgrade connection: container not found", rc=1)
        self.assertIn(
            f"WARNING: In-flight note (compliance-rbac-overgrant rep 2): kubectl exec into {NAMESPACE}/{POD} exited 1 (error: unable to upgrade connection: container not found)",
            result.stderr,
        )
        self.assertIn("a repetition refused at start prints START REFUSED naming it", result.stderr)
        self.assertNotIn("removed", result.stdout)
        self.assertIn("RC=0", result.stdout)


class NamesTest(unittest.TestCase):
    """The pod, container, path and grace come from the code that defines them."""

    def test_the_pod_and_container_are_the_operators(self):
        manifests = OPERATOR_MANIFESTS.read_text(encoding="utf-8")
        self.assertIn('return agent.Name + "-shell"', manifests)
        self.assertRegex(manifests, r'Name:\s+"shell",')
        self.assertIn("statefulset/platform-agent-shell", DEPLOY.read_text(encoding="utf-8"))
        self.assertEqual(lifted_line(CONSTANT_LINES[0]), 'readonly EVAL_SANDBOX_CONTAINER="shell"')

    def test_the_note_path_is_the_one_start_writes(self):
        self.assertEqual(lifted_line(CONSTANT_LINES[1]), 'readonly EVAL_SANDBOX_SCRATCH_DIR="/opt/data/scratch"')
        function = lifted("release_inflight_note")
        self.assertIn('note="${EVAL_SANDBOX_SCRATCH_DIR}/inflight_${audit_id}.json"', function)
        self.assertNotIn(".lock", function)
        script = AUDIT_REPORT.read_text(encoding="utf-8")
        if "def inflight_path_for" not in script:
            self.skipTest("audit_report.py carries no in-flight note on this tree; the path is pinned once the guard lands")
        self.assertIn('f"{SCRATCH_DIR}/inflight_{audit_id}.json"', script)
        self.assertIn('or "/opt/data/scratch"', script)

    def test_the_lock_deadline_grants_a_unit_its_grace_on_top_of_the_ceiling(self):
        # A same-task waiter's deadline is the holder's ceiling plus grading
        # and teardown; a holder that spends the grace before its run must
        # not push the waiter past it, so the grace is in the deadline.
        self.assertIn(
            'lock_deadline="$(( $(stream_case_count "${audit_id}") * ($(unit_delegation_timeout "${name}") + 600 + EVAL_INFLIGHT_GRACE_SECONDS) ))"',
            lifted("run_one_unit"),
        )
        self.assertEqual(grace_seconds(), 300)


class CallSiteTest(unittest.TestCase):
    """Where the release sits in run_one_unit, by its text."""

    def test_the_release_follows_the_ledger_reset_inside_the_locks_before_devops_bench(self):
        unit = lifted("run_one_unit")
        stream_lock = unit.index('lock_acquire "${STATE_DIR}/lock-stream-${audit_id}"')
        reset = unit.index('reset_audit_ledgers "${name} rep ${rep}" "${audit_id}"')
        release = unit.index('release_inflight_note "${name} rep ${rep}" "${audit_id}"')
        launch = unit.index("uv run devops-bench")
        stream_release = unit.index('lock_release "${STATE_DIR}/lock-stream-${audit_id}"', launch)
        self.assertLess(stream_lock, reset)
        self.assertLess(reset, release)
        self.assertLess(release, launch)
        self.assertLess(launch, stream_release)
        # Both inside the one block gated on the case writing a ledger.
        block = re.search(r'  if \[ -n "\$\{audit_id\}" \]; then\n(.*?)\n  fi\n', unit, re.DOTALL).group(1)
        self.assertIn("reset_audit_ledgers", block)
        self.assertIn("release_inflight_note", block)

    def test_the_release_runs_per_unit_and_nowhere_else(self):
        src = SCRIPT.read_text(encoding="utf-8")
        calls = re.findall(r"^\s*release_inflight_note .*$", src, re.MULTILINE)
        self.assertEqual(calls, ['    release_inflight_note "${name} rep ${rep}" "${audit_id}"'])
        # Defined after the ledger reset it sits beside, before the matrix;
        # its constants with the script's other readonly ones, above both.
        self.assertLess(src.index("reset_audit_ledgers() {"), src.index("release_inflight_note() {"))
        self.assertLess(src.index("release_inflight_note() {"), src.index("# 6. Task Matrix Execution Loop"))
        self.assertLess(src.index("readonly EVAL_INFLIGHT_GRACE_SECONDS="), src.index("reset_audit_ledgers() {"))


if __name__ == "__main__":
    unittest.main()
