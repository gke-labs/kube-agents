"""Case selection and the baseline record, offline."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from kube_agents_bench import priors as priors_mod
from kube_agents_bench import selection

TESTS = Path(__file__).parent
ROOT = selection.repo_root(TESTS)
BENCH = ROOT / "bench"

# A noop-deployer case with no fixtures and no shared-artifact check.
FREE_CASE = "agent-kanban-smoke"
# A noop case that reads the seeded fleet.
FLEET_CASE = "cost-idle-pool-probe"
# A case that provisions a tofu stack.
STACK_CASE = "gpu-stress-test-diagnosis"
# A case whose checks read GitHub.
LEDGER_CASE = "compliance-rbac-overgrant"


class SelectionTest(unittest.TestCase):
    def test_an_id_a_glob_and_a_path_resolve_once_each(self):
        cases = selection.select_cases(
            [FREE_CASE, "agent-kanban-*", str(BENCH / "tasks" / FREE_CASE / "task.yaml")], root=ROOT
        )
        self.assertEqual([c.case_id for c in cases], [FREE_CASE])

    def test_a_typo_is_an_error_not_an_empty_run(self):
        with self.assertRaises(selection.SelectionError):
            selection.select_cases(["no-such-case-*"], root=ROOT)
        with self.assertRaises(selection.SelectionError):
            selection.select_cases([], root=ROOT)

    def test_rosters_resolve_to_the_files_ci_reads(self):
        presubmit = selection.select_cases([], roster="presubmit", root=ROOT)
        blocking = selection.select_cases([], roster="blocking", root=ROOT)
        everything = selection.select_cases([], roster=selection.ROSTER_ALL, root=ROOT)
        self.assertTrue(presubmit)
        self.assertTrue({c.case_id for c in blocking} <= {c.case_id for c in presubmit})
        self.assertGreater(len(everything), len(presubmit))
        self.assertEqual(len({c.case_id for c in everything}), len(everything))

    def test_domain_selects_by_the_task_files_slug(self):
        cases = selection.select_cases([], domains=["cost"], root=ROOT)
        self.assertTrue(cases)
        self.assertTrue(all(c.spec.domain == "cost" for c in cases))

    def test_scheduling_metadata_comes_from_the_task_file(self):
        by_id = {
            c.case_id: c
            for c in selection.select_cases([FREE_CASE, FLEET_CASE, STACK_CASE, LEDGER_CASE], root=ROOT)
        }
        self.assertFalse(by_id[FREE_CASE].reads_fleet)
        self.assertFalse(by_id[FREE_CASE].exclusive)
        self.assertTrue(by_id[FLEET_CASE].reads_fleet)
        self.assertTrue(by_id[STACK_CASE].has_stack)
        self.assertTrue(by_id[STACK_CASE].exclusive)
        self.assertTrue(by_id[LEDGER_CASE].writes_shared_artifact)
        self.assertTrue(by_id[LEDGER_CASE].exclusive)

    def test_a_task_path_outside_the_tree_is_refused_not_substituted(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / FREE_CASE
            copy.mkdir()
            (copy / "task.yaml").write_text((BENCH / "tasks" / FREE_CASE / "task.yaml").read_text())
            with self.assertRaises(selection.SelectionError):
                selection.select_cases([str(copy / "task.yaml")], root=ROOT)
            with self.assertRaises(selection.SelectionError):
                selection.select_cases([str(copy)], root=ROOT)

    def test_roster_entries_drop_comments(self):
        text = "# header\n./tasks/a/task.yaml  # note\n\n  ./tasks/b/task.yaml\n#./tasks/c/task.yaml\n"
        self.assertEqual(selection.roster_entries(text), ["./tasks/a/task.yaml", "./tasks/b/task.yaml"])


class PriorsTest(unittest.TestCase):
    def _load(self, doc: dict) -> dict[str, priors_mod.Prior]:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(doc, fh)
        try:
            return priors_mod.load_priors(fh.name)
        finally:
            os.unlink(fh.name)

    def test_dashboard_prefers_a_full_nightly_record_and_falls_back_to_the_presubmit(self):
        doc = {
            "cases": [
                # A young nightly: the presubmit's pooled record is used.
                {"name": "a", "runs_on_record": 100, "pass_rate": 0.87, "durations": {"med": 300},
                 "nightly": {"runs_on_record": 4, "pass_rate": 1.0}},
                # A full nightly window: main's record wins.
                {"name": "b", "runs_on_record": 100, "pass_rate": 0.5,
                 "nightly": {"runs_on_record": 21, "pass_rate": 0.9}},
                # Nightly-only: no presubmit record at all, the nightly is used thin.
                {"name": "c", "runs_on_record": 0, "pass_rate": None,
                 "nightly": {"runs_on_record": 4, "pass_rate": 0.25}},
                {"name": "d", "runs_on_record": 0, "pass_rate": None},
            ]
        }
        loaded = self._load(doc)
        self.assertEqual(set(loaded), {"a", "b", "c"})
        self.assertEqual((loaded["a"].passes, loaded["a"].n, loaded["a"].median_seconds), (87, 100, 300.0))
        self.assertTrue(loaded["a"].source.endswith(priors_mod.TIER_PRESUBMIT))
        self.assertEqual((loaded["b"].passes, loaded["b"].n), (19, 21))
        self.assertTrue(loaded["b"].source.endswith(priors_mod.TIER_NIGHTLY))
        self.assertEqual((loaded["c"].passes, loaded["c"].n), (1, 4))

    def test_a_run_set_summary_is_a_baseline(self):
        doc = {
            "cases": [
                {"case_id": "a", "passes": 2, "fails": 1,
                 "reps": [{"outcome": "pass", "seconds": 10}, {"outcome": "fail", "seconds": 30},
                          {"outcome": "pass", "seconds": 20}, {"outcome": "blocked", "seconds": 1}]},
                {"case_id": "b", "passes": 0, "fails": 0, "reps": []},
            ]
        }
        loaded = self._load(doc)
        self.assertEqual(set(loaded), {"a"})
        self.assertEqual((loaded["a"].passes, loaded["a"].n, loaded["a"].median_seconds), (2, 3, 20.0))

    def test_none_is_empty_and_a_missing_file_is_an_error(self):
        self.assertEqual(priors_mod.load_priors(priors_mod.BASELINE_NONE), {})
        with self.assertRaises(priors_mod.PriorsError):
            priors_mod.load_priors("/no/such/summary.json")

    def test_gs_reads_are_cached_for_the_ttl(self):
        calls: list[str] = []

        def reader(url: str) -> str:
            calls.append(url)
            return '{"cases": []}'

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            url = "gs://bucket/evals/data.json"
            priors_mod._read_cached_gs(url, cache_dir=cache, ttl_s=3600, reader=reader)
            priors_mod._read_cached_gs(url, cache_dir=cache, ttl_s=3600, reader=reader)
            self.assertEqual(len(calls), 1)
            priors_mod._read_cached_gs(url, cache_dir=cache, ttl_s=0, reader=reader)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
