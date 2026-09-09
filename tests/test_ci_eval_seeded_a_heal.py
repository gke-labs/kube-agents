"""The seeded-a heal in hack/ci-eval-pr.sh (section 2c).

The seeded fleet's slot-a cluster runs the namespace-level defect workloads
on one e2-medium whose allocatable CPU GKE's system pods fully claim; a node
rebuild schedules the system set first and the fixtures go Pending (#1278).
Section 2c discovers slot a by the fleet labels and the trailing ``-a`` name
rule and resizes its default pool to two nodes when it is short.

These tests pin, with a recording ``gcloud`` stub (the approach of
tests/test_ci_deploy_release_guard.py):

* a one-node pool is resized to two, naming the discovered cluster and
  location, and the heal is announced;
* a two-node (or larger) pool is left alone -- no resize call;
* no slot-a cluster in the project is a no-op, not a failure;
* a failed ``clusters list`` is the same no-op -- under ``set -euo pipefail``
  the listing's assignment would otherwise kill the run;
* an unreadable node count leaves the pool alone with a warning;
* a failed resize warns and exits 0 -- the heal is never the reason a run
  dies;
* ``FLEET_HEAL_SEEDED_A=0`` issues no gcloud call at all.
"""

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

_CI_EVAL = pathlib.Path(__file__).resolve().parents[1] / "hack" / "ci-eval-pr.sh"
_HEAL_START = "# ─── 2c. Heal seeded-a's default pool"
_HEAL_END = "# 3. Agent & Harness Configuration"

_CLUSTERS = "evals-2-seeded-a\tus-central1-a\nevals-2-seeded-b\tus-central1-a\nevals-2-seeded-c\tus-central1-a\n"


def _heal_block():
    text = _CI_EVAL.read_text(encoding="utf-8")
    start = text.find(_HEAL_START)
    assert start != -1, f"{_HEAL_START!r} not found in hack/ci-eval-pr.sh"
    end = text.find(_HEAL_END, start)
    assert end != -1, f"{_HEAL_END!r} not found after the heal"
    return text[start:end]


class SeededAHealTest(unittest.TestCase):
    maxDiff = None

    def _run(self, clusters=_CLUSTERS, nodes="1", list_exit=0, describe_exit=0, resize_exit=0, env_extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            calls = tmp / "calls.log"
            (tmp / "clusters.txt").write_text(clusters, encoding="utf-8")
            stub = bin_dir / "gcloud"
            stub.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "$*" >> "{calls}"\n'
                'case "$*" in\n'
                f'  *"clusters list"*) [ {list_exit} -eq 0 ] && cat "{tmp / "clusters.txt"}"; exit {list_exit} ;;\n'
                f'  *"node-pools describe"*) [ {describe_exit} -eq 0 ] && printf "%s\\n" "{nodes}"; exit {describe_exit} ;;\n'
                f'  *"clusters resize"*) exit {resize_exit} ;;\n'
                "esac\n",
                encoding="utf-8",
            )
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "PROJECT_ID": "kube-agents-evals-2"}
            env.update(env_extra or {})
            proc = subprocess.run(
                ["bash", "-c", "set -euo pipefail\n" + _heal_block()],
                env=env, capture_output=True, text=True, check=False,
            )
            log = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return proc, log

    def test_a_short_pool_is_resized_to_two_and_announced(self):
        proc, log = self._run(nodes="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clusters resize evals-2-seeded-a --node-pool default-pool --num-nodes 2 --location us-central1-a --project kube-agents-evals-2 --quiet", log)
        self.assertIn("HEALED kube-agents-evals-2/evals-2-seeded-a/default-pool 1 -> 2 nodes", proc.stdout)

    def test_a_healthy_pool_is_left_alone(self):
        for nodes in ("2", "3"):
            proc, log = self._run(nodes=nodes)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("clusters resize", log)
            self.assertIn(f"already has {nodes} node(s)", proc.stdout)

    def test_no_slot_a_cluster_is_a_noop(self):
        proc, log = self._run(clusters="evals-2-seeded-b\tus-central1-a\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("node-pools describe", log)
        self.assertNotIn("clusters resize", log)
        self.assertIn("nothing to heal", proc.stdout)

    def test_a_failed_cluster_list_is_a_noop_not_a_death(self):
        # The block runs under `set -euo pipefail`; without the `|| true` on
        # the listing, a non-zero `clusters list` trips errexit on the
        # assignment and the run dies at 2c instead of warning.
        proc, log = self._run(list_exit=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("node-pools describe", log)
        self.assertNotIn("clusters resize", log)
        self.assertIn("nothing to heal", proc.stdout)

    def test_an_unreadable_count_warns_and_leaves_the_pool_alone(self):
        proc, log = self._run(describe_exit=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("clusters resize", log)
        self.assertIn("could not read", proc.stderr)

    def test_a_failed_resize_warns_and_never_kills_the_run(self):
        proc, log = self._run(nodes="1", resize_exit=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clusters resize", log)
        self.assertIn("resize of evals-2-seeded-a/default-pool failed", proc.stderr)

    def test_the_escape_hatch_issues_no_calls(self):
        proc, log = self._run(nodes="1", env_extra={"FLEET_HEAL_SEEDED_A": "0"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log, "")


if __name__ == "__main__":
    unittest.main()
