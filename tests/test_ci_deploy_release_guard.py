"""The deploy heals a poisoned Helm release record before `upgrade --install`.

#1172: a pool project can arrive at deploy carrying a `kube-agents` release
record with no deployed revision — teardown's `helm uninstall` failed and
left the record behind, or teardown was killed mid-uninstall, which no
teardown-side fallback can cover. `helm upgrade --install` then takes the
upgrade path and dies with `UPGRADE FAILED: "kube-agents" has no deployed
releases`, instantly failing whichever PR drew the project. The guard in
`hack/ci-deploy.sh` probes `helm history -o json` — the same release-record
Secrets whose statuses Helm's upgrade path queries — and clears the record
(uninstall `--no-hooks`, falling back to deleting the record Secrets) when
the release exists with no `deployed` revision.

These tests pin:

* an absent release (fresh project) and a healthy release (any revision
  `deployed`) are left alone — one probe call, no uninstall, no delete;
* a release whose revisions are all failed/pending is uninstalled
  `--no-hooks`, and a successful uninstall issues no Secret delete;
* when even the uninstall fails, the record Secrets are deleted by the
  `owner=helm,name=kube-agents` selector with --ignore-not-found;
* when both clears fail, the guard aborts under `set -e` rather than
  letting the upgrade fail less legibly with the record still in place;
* at lease time, a transient probe 5xx re-probes and heals the record on success,
  while a persistent probe 5xx warns and returns 0 rather than aborting under `set -e`.

The guard is lifted from the script's own text and executed under bash with
stubbed helm/kubectl, the same approach as tests/test_ci_teardown_sweep.py.
"""

import json
import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

# The guard's own boundaries in hack/ci-deploy.sh. Asserted present below, so
# moving the block fails here loudly instead of silently testing nothing.
_GUARD_START = "# ─── 5a. Heal a poisoned release record"
_GUARD_END = "API_SERVER_KEY="

# Repeated here on purpose rather than parsed out of the script: a test that
# derives the expected selector from the code under test asserts nothing.
_RECORD_SELECTOR = "owner=helm,name=kube-agents"

_NAMESPACE = "kubeagents-system"

# helm history -o json output shapes, as the real encoder emits them
# (compact keys, one object per revision).
_HISTORY_HEALTHY = json.dumps(
    [
        {"revision": 1, "status": "superseded"},
        {"revision": 2, "status": "deployed"},
        {"revision": 3, "status": "failed"},
    ]
)
_HISTORY_POISONED = json.dumps(
    [
        {"revision": 1, "status": "pending-install"},
        {"revision": 2, "status": "failed"},
    ]
)


def _deploy_text():
    return _CI_DEPLOY.read_text(encoding="utf-8")


def _head_constants(text):
    """The file-head `readonly` declarations the lifted guard reads."""
    head = text[: text.find(_GUARD_START)]
    return "\n".join(
        line for line in head.splitlines() if line.startswith("readonly ")
    )


def _shell_function(text, name):
    """A function of the script, from its `name() {` line to the `}` that
    closes it in column 0."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == f"{name}() {{":
            for end in range(index, len(lines)):
                if lines[end] == "}":
                    return "\n".join(lines[index : end + 1])
            break
    raise AssertionError(f"{name}() not found in hack/ci-deploy.sh in the shape this test lifts")


def _guard_block(text):
    start = text.find(_GUARD_START)
    assert start != -1, f"{_GUARD_START!r} not found in hack/ci-deploy.sh"
    end = text.find(_GUARD_END, start)
    assert end != -1, f"{_GUARD_END!r} not found after the guard"
    return text[start:end]


class CiDeployReleaseGuardTest(unittest.TestCase):
    maxDiff = None

    def _run_guard(
        self,
        history_json="",
        history_exit=0,
        uninstall_exit=0,
        kubectl_exit=0,
        history_responses=None,
    ):
        """Run the lifted guard with recording stubs.

        Returns (returncode, call argv lines, stdout, stderr). The helm stub
        answers `history` with the given JSON and exit code and `uninstall`
        with the given exit code; kubectl records and exits as asked.
        """
        if history_responses is None:
            history_responses = [(history_exit, history_json, "")]

        text = _deploy_text()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log = tmp_path / "calls.log"
            log.touch()

            history_responses_file = tmp_path / "history_responses.json"
            history_responses_file.write_text(json.dumps(history_responses), encoding="utf-8")
            history_index_file = tmp_path / "history_index.txt"
            history_index_file.write_text("0", encoding="utf-8")

            helm_stub = bin_dir / "helm"
            helm_stub.write_text(
                f"""#!/usr/bin/env bash
echo "helm $*" >> "{log}"
case "$1" in
  history)
    h_idx=$(cat "{history_index_file}")
    h_resp=$(python3 -c '
import json, sys
data = json.load(open("{history_responses_file}"))
idx = int(sys.argv[1])
if idx < len(data):
    code, out, err = data[idx]
else:
    code, out, err = data[-1]
if out: sys.stdout.write(out + "\\n")
if err: sys.stderr.write(err + "\\n")
sys.exit(code)
' "$h_idx")
    h_rc=$?
    echo $((h_idx + 1)) > "{history_index_file}"
    if [ -n "$h_resp" ]; then
        printf "%s\\n" "$h_resp"
    fi
    exit $h_rc
    ;;
  uninstall)
    exit {uninstall_exit}
    ;;
  *)
    exit 0
    ;;
esac
""",
                encoding="utf-8",
            )
            kubectl_stub = bin_dir / "kubectl"
            kubectl_stub.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "kubectl $*" >> "{log}"\n'
                f"exit {kubectl_exit}\n",
                encoding="utf-8",
            )
            for stub in (helm_stub, kubectl_stub):
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            # set -euo pipefail mirrors the script's own header; NAMESPACE is
            # the one variable the lifted guard reads that the head sets.
            proc = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -euo pipefail\n"
                    + _head_constants(text)
                    + "\n"
                    + _shell_function(text, "heal_poisoned_release_record")
                    + "\n"
                    + _guard_block(text),
                ],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                env=get_isolated_test_env(
                    bin_dir=bin_dir, overrides={"NAMESPACE": _NAMESPACE}
                ),
            )
            calls = log.read_text(encoding="utf-8").splitlines()
        return proc.returncode, calls, proc.stdout, proc.stderr

    def _helm_uninstalls(self, calls):
        return [c for c in calls if c.startswith("helm uninstall")]

    def _record_deletes(self, calls):
        return [
            c
            for c in calls
            if c.startswith("kubectl delete secret") and _RECORD_SELECTOR in c.split()
        ]

    # --- healthy and absent releases pay one probe and nothing else ---------

    def test_an_absent_release_is_left_alone(self):
        """A fresh project: `helm history` fails (release: not found)."""
        rc, calls, out, err = self._run_guard(history_json="", history_exit=1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._helm_uninstalls(calls), [])
        self.assertEqual(self._record_deletes(calls), [])
        self.assertEqual(
            len(calls), 1, f"a fresh project must cost exactly the probe: {calls}"
        )

    def test_a_release_with_a_deployed_revision_is_left_alone(self):
        """Any `deployed` revision means the upgrade path works, even with a
        failed revision on top of it."""
        rc, calls, out, err = self._run_guard(history_json=_HISTORY_HEALTHY)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._helm_uninstalls(calls), [])
        self.assertEqual(self._record_deletes(calls), [])
        self.assertEqual(
            len(calls), 1, f"a healthy release must cost exactly the probe: {calls}"
        )

    # --- the poisoned record is healed ---------------------------------------

    def test_a_poisoned_release_is_uninstalled_without_hooks(self):
        rc, calls, out, err = self._run_guard(history_json=_HISTORY_POISONED)
        self.assertEqual(rc, 0, err)
        uninstalls = self._helm_uninstalls(calls)
        self.assertEqual(
            len(uninstalls), 1, f"expected one uninstall of the poisoned record: {calls}"
        )
        argv = uninstalls[0].split()
        self.assertIn("--no-hooks", argv)
        self.assertIn("-n", argv)
        self.assertIn(_NAMESPACE, argv)
        # The uninstall succeeded, so the Secret-level fallback stays unused.
        self.assertEqual(self._record_deletes(calls), [])
        # Lease time never touches the CR: no operator is running to clear its
        # finalizer, so a --wait delete here would time out and abort the run.
        self.assertEqual(
            [c for c in calls if c.startswith("kubectl delete platformagent")], []
        )
        # And the heal is loud: a later reader of a red run's log must see it.
        self.assertIn("poisoned", out.lower())

    def test_a_failed_uninstall_falls_back_to_deleting_the_record_secrets(self):
        rc, calls, out, err = self._run_guard(
            history_json=_HISTORY_POISONED, uninstall_exit=1
        )
        self.assertEqual(rc, 0, err)
        deletes = self._record_deletes(calls)
        self.assertEqual(
            len(deletes), 1, f"expected the record-Secret fallback to fire: {calls}"
        )
        argv = deletes[0].split()
        self.assertIn("-n", argv)
        self.assertIn(_NAMESPACE, argv)
        self.assertIn("--ignore-not-found", argv)

    def test_both_clears_failing_aborts_before_the_upgrade(self):
        """Uninstall red AND the record delete red: the record is still in
        place, so `set -e` must stop the run here rather than let the
        upgrade fail less legibly — a later `|| true` on that line would
        ship this regression silently."""
        rc, calls, out, err = self._run_guard(
            history_json=_HISTORY_POISONED, uninstall_exit=1, kubectl_exit=1
        )
        self.assertNotEqual(rc, 0, "a guard that cannot clear the record must abort")

    # --- probe 5xx error handling at lease time --------------------------------

    def test_lease_time_transient_probe_5xx_reprobes_and_heals_poisoned_release_record(self):
        """At lease time (§5a), a transient 5xx on probe attempt 1 re-probes and heals
        the poisoned release record when attempt 2 succeeds."""
        err_msg = "Error: the server is currently unable to handle the request"
        history_responses = [
            (1, "", err_msg),
            (0, _HISTORY_POISONED, ""),
        ]
        rc, calls, out, err = self._run_guard(history_responses=history_responses)
        self.assertEqual(rc, 0, err)
        uninstalls = self._helm_uninstalls(calls)
        self.assertEqual(
            len(uninstalls), 1, f"expected one uninstall: {calls}"
        )
        history_calls = [c for c in calls if c.startswith("helm history")]
        self.assertEqual(len(history_calls), 2, f"expected 2 history calls: {calls}")
        self.assertEqual(
            [c for c in calls if c.startswith("kubectl delete platformagent")], []
        )
        self.assertIn("hit a transient api-server 5xx", out.lower())
        self.assertIn("cleared the poisoned", out.lower())

    def test_lease_time_persistent_probe_5xx_warns_and_proceeds(self):
        """At lease time (§5a), if the API server persistently 5xx's across all probe
        attempts, the guard warns and returns 0 rather than aborting under set -e,
        allowing the run to proceed to step 5c where deploy retries absorb the blip."""
        err_msg = "Error: the server is currently unable to handle the request"
        history_responses = [
            (1, "", err_msg),
            (1, "", err_msg),
            (1, "", err_msg),
        ]
        rc, calls, out, err = self._run_guard(history_responses=history_responses)
        self.assertEqual(rc, 0, f"lease-time probe 5xx must not abort under set -e: {err}")
        self.assertEqual(self._helm_uninstalls(calls), [])
        self.assertEqual(self._record_deletes(calls), [])
        history_calls = [c for c in calls if c.startswith("helm history")]
        self.assertEqual(len(history_calls), 3, f"expected 3 history calls: {calls}")
        self.assertIn("skipping lease-time release record heal and proceeding to deploy", out.lower())

    # --- the file itself ------------------------------------------------------

    def test_ci_deploy_parses(self):
        subprocess.run(["bash", "-n", str(_CI_DEPLOY)], check=True)


if __name__ == "__main__":
    unittest.main()
