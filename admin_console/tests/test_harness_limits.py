from __future__ import annotations

import json
import math
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from admin_console import harness_limits as hl
from admin_console.charts import harness_limit_chart

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "admin_console" / "app.py"
PAGE_PATH = "views/harness_limits.py"
PAGE_SOURCE = REPO_ROOT / "admin_console" / "views" / "harness_limits.py"
MODULE_SOURCE = REPO_ROOT / "admin_console" / "harness_limits.py"
PLAN_PATH = REPO_ROOT / "bench" / "scale" / "phase1.yaml"
SCALE_README = REPO_ROOT / "bench" / "scale" / "README.md"
MIB = 1024**2
GIB = 1024**3
BASE_TOTALS = {"clusters": 2, "nodes": 4}
# A run-level metric without a threshold, for the fitting and prediction tests.
RUN_METRIC = hl.WATCHER_SEEN
PROMPT_FAILURE = "prompt failed: no answer within 2400s"
TEST_PROJECT = "test-project-01"


def record(
    factor: str = "base",
    level: float = 0,
    operation: str = "fleet_audit",
    run: int = 1,
    *,
    metrics: tuple = (),
    totals: dict | None = None,
    limits: dict | None = None,
    excluded: tuple = (),
    commit: str = "a" * 40,
    mode: str = "next",
    started: str = "",
    **optional,
) -> dict:
    """A record as the runner writes it; ``optional`` adds image, values, replicas or errors."""
    return {
        "key": f"{commit[:8]}|{mode}|{factor}|{level:g}|{operation}|{run}",
        "commit": commit,
        "mode": mode,
        "phase": "phase1",
        "factor": factor,
        "level": level,
        "operation": operation,
        "run": run,
        "started": started or f"2026-10-23T{run:02d}:00:00Z",
        "ended": f"2026-10-23T{run:02d}:30:00Z",
        "totals": dict(BASE_TOTALS) if totals is None else totals,
        "limits": limits or {},
        "metrics": [
            {"name": name, "value": value, **({"container": container} if container else {})}
            for name, container, value in metrics
        ],
        "provider_429s": 1 if excluded else 0,
        "excluded_from": list(excluded),
        **optional,
    }


def planned(**changes: float) -> dict:
    """Every factor's planned value: the plan's base point with ``changes`` applied."""
    return {**hl.PLAN_BASE_LEVELS, **changes}


def parse(*items: dict) -> tuple[hl.Record, ...]:
    return hl.parse_records({"schema": 1, "records": list(items)})


def point(x: float, y: float, *, excluded: bool = False) -> hl.Point:
    return hl.Point(x, y, f"{x}:{y}", 1, False, excluded)


def memory(container: str, mib: float) -> tuple:
    return (hl.MEMORY_PEAK, container, mib * MIB)


def clusters_line(base: float = 100.0, at_eight: float = 160.0) -> tuple[hl.Record, ...]:
    """A metric of 100 at the base (2 clusters) and 160 at 8: 10 per cluster."""
    return parse(
        record(metrics=((RUN_METRIC, None, base),)),
        record(
            "clusters",
            8,
            metrics=((RUN_METRIC, None, at_eight),),
            totals={"clusters": 8, "nodes": 16},
        ),
    )


def recommendation(container: str, memory_mib: float | None, cpu: float | None):
    """A recommendation whose predicted peaks are the given values, before the margin."""

    def peak(metric: str, value: float | None) -> hl.Prediction | None:
        return None if value is None else hl.Prediction(metric, container, value, "fleet_audit", ())

    return hl.Recommendation(
        container,
        peak(hl.MEMORY_PEAK, None if memory_mib is None else memory_mib * MIB),
        peak(hl.CPU_PEAK, cpu),
    )


class LoadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write(self, name: str, records: list[dict], schema: int = hl.SCHEMA_VERSION) -> Path:
        path = self.root / name
        path.write_text(json.dumps({"schema": schema, "records": records}))
        return path

    def test_the_measured_file_wins_over_the_sample(self):
        data = self.write("data.json", [record()])
        sample = self.write("sample.json", [record(), record(run=2)])

        dataset = hl.load_dataset(data, sample)

        self.assertFalse(dataset.is_sample)
        self.assertEqual(dataset.path, data)
        self.assertEqual(len(dataset.records), 1)

    def test_a_missing_measured_file_loads_the_sample(self):
        sample = self.write("sample.json", [record(), record(run=2)])

        dataset = hl.load_dataset(self.root / "missing.json", sample)

        self.assertTrue(dataset.is_sample)
        self.assertEqual(len(dataset.records), 2)

    def test_another_schema_version_is_refused(self):
        data = self.write("data.json", [record()], schema=2)

        with self.assertRaisesRegex(hl.HarnessDataError, "schema 2, expected 1"):
            hl.load_dataset(data)

    def test_a_malformed_record_names_its_key(self):
        broken = record()
        broken["metrics"] = "none"

        with self.assertRaisesRegex(hl.HarnessDataError, r"aaaaaaaa\|next\|base\|0"):
            parse(broken)

    def test_a_record_without_its_identity_is_refused(self):
        broken = record()
        del broken["operation"]

        with self.assertRaisesRegex(hl.HarnessDataError, "operation is missing"):
            parse(broken)

    def test_a_reading_that_is_not_finite_is_dropped(self):
        (loaded,) = parse(record(metrics=((RUN_METRIC, None, math.nan), (hl.RESPONSE, None, 3.0))))

        self.assertIsNone(loaded.value(RUN_METRIC))
        self.assertEqual(loaded.value(hl.RESPONSE), 3.0)

    def test_fields_outside_the_schema_are_ignored(self):
        extra = record(metrics=((RUN_METRIC, None, 1.0),))
        extra["notes"] = "written by a later runner"

        (loaded,) = parse(extra)

        self.assertEqual(loaded.value(RUN_METRIC), 1.0)

    def test_a_record_without_the_newer_fields_still_loads(self):
        (loaded,) = parse(record())

        self.assertEqual(loaded.image, "")
        self.assertEqual(dict(loaded.values), {})
        self.assertEqual(dict(loaded.replicas), {})
        self.assertEqual(loaded.errors, ())

    def test_image_values_replicas_and_errors_load(self):
        image = "ghcr.io/gke-labs/kube-agents/platform-agent:" + "a" * 40
        (loaded,) = parse(
            record(
                image=image,
                values=planned(clusters=8),
                replicas={"litellm-container": 2},
                errors=["monitoring: TimeoutError: timed out"],
            )
        )

        self.assertEqual(loaded.image, image)
        self.assertEqual(loaded.values[hl.CLUSTERS], 8)
        self.assertEqual(loaded.values[hl.FAILING_PODS], 1)
        self.assertEqual(loaded.replicas["litellm-container"], 2)
        self.assertEqual(loaded.errors, ("monitoring: TimeoutError: timed out",))

    def test_errors_that_are_not_strings_are_refused(self):
        with self.assertRaisesRegex(hl.HarnessDataError, "errors is not a list of strings"):
            parse(record(errors=[{"reason": "timeout"}]))

    def test_the_checked_in_sample_is_clearly_fake_and_two_point(self):
        records = hl.load_dataset(REPO_ROOT / "missing.json").records

        self.assertEqual({item.commit for item in records}, {"0" * 40})
        self.assertEqual({item.phase for item in records}, {"sample"})
        self.assertEqual(
            hl.factors(records),
            [hl.CLUSTERS, hl.NODES, hl.OBJECTS, hl.FAILING_PODS, hl.CONCURRENT],
        )
        self.assertTrue(any(item.is_base for item in records))
        for item in records:
            with self.subTest(record=item.key):
                self.assertEqual(set(item.values), set(hl.FACTORS))
                self.assertTrue(item.image.endswith(item.commit))
        for operation in hl.operations(records):
            for factor in hl.factors(records, operation):
                with self.subTest(operation=operation, factor=factor):
                    points = hl.series_points(
                        records, operation, factor, hl.MEMORY_PEAK, "agent-api-auth"
                    )
                    self.assertTrue(hl.fit_line(points).two_point)


class FactorValueTest(unittest.TestCase):
    def test_a_total_named_after_the_factor_is_the_measured_value(self):
        (loaded,) = parse(record("clusters", 8, totals={"clusters": 7}))

        self.assertEqual(hl.factor_value(loaded, hl.CLUSTERS), 7)

    def test_objects_are_the_design_kinds_per_cluster(self):
        totals = {
            "clusters": 2,
            "namespaces": 30,
            "deployments": 30,
            "services": 46,
            "hpas": 30,
            "pods": 94,
            "configmaps": 126,
            "events": 5000,
        }
        (loaded,) = parse(record("objects", 4000, totals=totals))

        # 356 objects over 2 clusters; events are not objects.
        self.assertEqual(hl.factor_value(loaded, hl.OBJECTS), 178)

    def test_a_missing_total_falls_back_to_the_level(self):
        (loaded,) = parse(record(hl.CHURN, 1000))

        self.assertEqual(hl.factor_value(loaded, hl.CHURN), 1000)

    def test_a_per_cluster_total_is_divided_by_the_cluster_count(self):
        (loaded,) = parse(record(hl.NODES, 100, totals={"clusters": 2, "nodes": 206}))

        self.assertEqual(hl.factor_value(loaded, hl.NODES), 103)

    def test_a_missing_per_cluster_total_falls_back_to_the_level(self):
        (loaded,) = parse(record(hl.NODES, 100, totals={"clusters": 2}))

        self.assertEqual(hl.factor_value(loaded, hl.NODES), 100)

    def test_a_factor_the_totals_do_not_count_sits_at_its_planned_value(self):
        failing, base = parse(
            record(hl.FAILING_PODS, 100, values=planned(failing_pods=100)),
            record(values=planned(churn_per_minute=20, concurrent=3)),
        )

        self.assertEqual(hl.factor_value(failing, hl.FAILING_PODS), 100)
        self.assertEqual(hl.factor_value(base, hl.FAILING_PODS), 1)
        # The record's planned values win over the plan's base point.
        self.assertEqual(hl.factor_value(base, hl.CHURN), 20)
        self.assertEqual(hl.factor_value(base, hl.CONCURRENT), 3)
        self.assertEqual(hl.factor_value(base, hl.LISTING_PODS), 100)

    def test_the_totals_win_over_the_planned_value(self):
        (loaded,) = parse(
            record(hl.CLUSTERS, 8, totals={"clusters": 7, "nodes": 21}, values=planned(clusters=8))
        )

        self.assertEqual(hl.factor_value(loaded, hl.CLUSTERS), 7)
        self.assertEqual(hl.factor_value(loaded, hl.NODES), 3)

    def test_only_a_base_record_without_values_takes_the_plan_base(self):
        without, partial = parse(record(), record(values={hl.CLUSTERS: 2}))

        self.assertEqual(hl.factor_value(without, hl.NODES), 2)
        self.assertEqual(hl.factor_value(without, hl.OBJECTS), hl.PLAN_BASE_LEVELS[hl.OBJECTS])
        self.assertEqual(hl.factor_value(without, hl.CHURN), hl.PLAN_BASE_LEVELS[hl.CHURN])
        self.assertIsNone(hl.factor_value(partial, hl.CHURN))

    def test_a_run_of_another_factor_is_not_on_the_axis(self):
        records = parse(
            record(metrics=((RUN_METRIC, None, 1.0),)),
            record("clusters", 8, metrics=((RUN_METRIC, None, 2.0),)),
            record(hl.CHURN, 1000, metrics=((RUN_METRIC, None, 3.0),)),
        )

        points = hl.series_points(records, "fleet_audit", hl.CHURN, RUN_METRIC)

        self.assertEqual([(item.x, item.y) for item in points], [(10.0, 1.0), (1000.0, 3.0)])


class FitTest(unittest.TestCase):
    def test_two_points_give_the_line_through_them(self):
        fit = hl.fit_line([point(2, 10), point(8, 40)])

        self.assertAlmostEqual(fit.slope, 5)
        self.assertAlmostEqual(fit.intercept, 0)
        self.assertTrue(fit.two_point)
        self.assertEqual(fit.label, "two-point estimate")

    def test_repeated_runs_at_two_levels_are_still_a_two_point_estimate(self):
        fit = hl.fit_line([point(2, 10), point(8, 40), point(8, 50)])

        self.assertAlmostEqual(fit.at(2), 10)
        self.assertAlmostEqual(fit.at(8), 45)
        self.assertTrue(fit.two_point)

    def test_three_levels_are_a_least_squares_fit(self):
        fit = hl.fit_line([point(0, 1), point(1, 3), point(2, 2)])

        self.assertAlmostEqual(fit.slope, 0.5)
        self.assertAlmostEqual(fit.intercept, 1.5)
        self.assertFalse(fit.two_point)
        self.assertEqual(fit.label, "least-squares fit")

    def test_one_level_has_no_fit(self):
        self.assertIsNone(hl.fit_line([point(2, 10), point(2, 12)]))

    def test_an_excluded_point_stays_out_of_the_fit(self):
        fit = hl.fit_line([point(2, 10), point(8, 40), point(8, 400, excluded=True)])

        self.assertAlmostEqual(fit.slope, 5)
        self.assertIsNone(hl.fit_line([point(2, 10), point(8, 400, excluded=True)]))

    def test_a_record_excludes_only_the_metrics_it_names(self):
        records = parse(
            record(metrics=((hl.RESPONSE, None, 20.0), memory("agent-api-auth", 400))),
            record(
                "clusters",
                8,
                metrics=((hl.RESPONSE, None, 900.0), memory("agent-api-auth", 700)),
                totals={"clusters": 8},
                excluded=(hl.RESPONSE,),
            ),
        )

        response = hl.series_points(records, "fleet_audit", hl.CLUSTERS, hl.RESPONSE)
        used = hl.series_points(
            records, "fleet_audit", hl.CLUSTERS, hl.MEMORY_PEAK, "agent-api-auth"
        )

        self.assertEqual([item.excluded for item in response], [False, True])
        self.assertIsNone(hl.fit_line(response))
        self.assertIsNotNone(hl.fit_line(used))

    def test_a_failed_prompt_leaves_the_run_out_of_the_response_fit_only(self):
        records = parse(
            record(metrics=((hl.RESPONSE, None, 20.0), memory("agent-api-auth", 400))),
            record(
                run=2,
                metrics=((hl.RESPONSE, None, 900.0), memory("agent-api-auth", 420)),
                errors=[PROMPT_FAILURE],
            ),
            record(
                "clusters",
                8,
                metrics=((hl.RESPONSE, None, 50.0), memory("agent-api-auth", 700)),
                totals={"clusters": 8},
                errors=["scrape litellm: timed out"],
            ),
        )

        response = hl.series_points(records, "fleet_audit", hl.CLUSTERS, hl.RESPONSE)
        used = hl.series_points(
            records, "fleet_audit", hl.CLUSTERS, hl.MEMORY_PEAK, "agent-api-auth"
        )
        prediction = hl.predict(records, hl.RESPONSE, None, hl.base_fleet(records))

        self.assertEqual([item.excluded for item in response], [False, True, False])
        self.assertFalse(any(item.excluded for item in used))
        # The base value is the run whose prompts all completed.
        self.assertAlmostEqual(prediction.value, 20)
        self.assertEqual(
            [row["Excluded from fit"] for row in hl.measurement_rows(records[1:2])],
            [True, False],
        )

    def test_the_transport_failure_marker_also_marks_a_failed_prompt(self):
        marked, unrelated = parse(
            record(errors=["KUBE_AGENTS_INFRA_FAILURE: relay closed"]),
            record(errors=["no running pod for app=litellm"]),
        )

        self.assertTrue(marked.excluded(hl.RESPONSE))
        self.assertFalse(marked.excluded(hl.MEMORY_PEAK))
        self.assertFalse(unrelated.excluded(hl.RESPONSE))


class PredictionTest(unittest.TestCase):
    def fleet(self, records, **inputs: float) -> dict[str, float]:
        return {**hl.base_fleet(records), **inputs}

    def test_at_the_base_point_the_prediction_is_the_measured_base(self):
        records = clusters_line()

        prediction = hl.predict(records, RUN_METRIC, None, hl.base_fleet(records))

        self.assertAlmostEqual(prediction.value, 100)
        self.assertEqual(hl.basis(prediction), "measured at the base point")

    def test_inside_the_measured_range_the_line_interpolates(self):
        records = clusters_line()

        prediction = hl.predict(records, RUN_METRIC, None, self.fleet(records, clusters=5))

        self.assertAlmostEqual(prediction.value, 130)
        self.assertEqual(
            hl.basis(prediction), "within the measured range; two-point estimate"
        )

    def test_past_the_largest_measured_value_it_is_predicted_not_measured(self):
        records = clusters_line()

        prediction = hl.predict(records, RUN_METRIC, None, self.fleet(records, clusters=20))

        self.assertAlmostEqual(prediction.value, 280)
        self.assertEqual(
            hl.basis(prediction),
            "predicted, not measured: clusters 20 > 8; two-point estimate",
        )

    def test_each_factor_adds_its_increase_from_its_own_base(self):
        records = parse(
            record(metrics=((RUN_METRIC, None, 100.0),)),
            record(
                "clusters", 8, metrics=((RUN_METRIC, None, 160.0),), totals={"clusters": 8, "nodes": 16}
            ),
            record(
                "nodes", 100, metrics=((RUN_METRIC, None, 198.0),), totals={"clusters": 2, "nodes": 200}
            ),
        )

        # Clusters add 10 for each of 6 more; nodes add 1 for each of 8 more per cluster.
        prediction = hl.predict(
            records, RUN_METRIC, None, self.fleet(records, clusters=8, nodes=10)
        )

        self.assertAlmostEqual(prediction.value, 168)
        self.assertEqual(
            hl.grown_factors(records, self.fleet(records, clusters=8, nodes=10)),
            [hl.CLUSTERS, hl.NODES],
        )
        self.assertEqual(hl.grown_factors(records, self.fleet(records, clusters=8)), [hl.CLUSTERS])
        self.assertEqual(hl.grown_factors(records, hl.base_fleet(records)), [])

    def test_the_largest_base_and_the_largest_increase_count(self):
        records = parse(
            record(metrics=((RUN_METRIC, None, 100.0),)),
            record(
                "clusters", 8, metrics=((RUN_METRIC, None, 130.0),), totals={"clusters": 8}
            ),
            record(operation="fan_out", metrics=((RUN_METRIC, None, 80.0),)),
            record(
                "clusters",
                8,
                operation="fan_out",
                metrics=((RUN_METRIC, None, 140.0),),
                totals={"clusters": 8},
            ),
        )

        prediction = hl.predict(records, RUN_METRIC, None, self.fleet(records, clusters=8))

        # The fleet audit's base (100) plus the fan-out's increase (60).
        self.assertEqual(prediction.base_operation, "fleet_audit")
        self.assertEqual(prediction.contributions[0].operation, "fan_out")
        self.assertAlmostEqual(prediction.value, 160)

    def test_an_excluded_base_run_stays_out_of_the_base_value(self):
        records = parse(
            record(metrics=((hl.RESPONSE, None, 20.0),)),
            record(run=2, metrics=((hl.RESPONSE, None, 500.0),), excluded=(hl.RESPONSE,)),
            record("clusters", 8, metrics=((hl.RESPONSE, None, 50.0),), totals={"clusters": 8}),
        )

        prediction = hl.predict(records, hl.RESPONSE, None, hl.base_fleet(records))

        self.assertAlmostEqual(prediction.value, 20)

    def test_without_base_runs_each_factors_lowest_level_is_the_base(self):
        records = parse(
            record("clusters", 2, metrics=((RUN_METRIC, None, 100.0),), totals={"clusters": 2}),
            record("clusters", 8, metrics=((RUN_METRIC, None, 160.0),), totals={"clusters": 8}),
        )

        prediction = hl.predict(records, RUN_METRIC, None, self.fleet(records, clusters=4))

        self.assertAlmostEqual(prediction.base, 100)
        self.assertAlmostEqual(prediction.value, 120)

    def test_without_base_runs_the_default_fleet_reads_the_planned_values(self):
        records = parse(
            record("clusters", 2, totals={"clusters": 2}, values=planned(churn_per_minute=20)),
            record(
                "clusters",
                8,
                totals={"clusters": 8},
                values=planned(clusters=8, churn_per_minute=20),
            ),
        )

        fleet = hl.base_fleet(records)

        self.assertEqual(fleet[hl.CLUSTERS], 2)
        self.assertEqual(fleet[hl.CHURN], 20)

    def test_a_factor_measured_at_one_level_is_reported_without_a_fit(self):
        records = parse(
            record(metrics=((RUN_METRIC, None, 100.0),)),
            record(hl.CHURN, 10, metrics=((RUN_METRIC, None, 101.0),)),
        )

        prediction = hl.predict(
            records, RUN_METRIC, None, self.fleet(records, churn_per_minute=1000)
        )

        self.assertEqual(prediction.missing_fits, (hl.CHURN,))
        self.assertIn("no fit for churn_per_minute", hl.basis(prediction))

    def test_a_factor_outside_the_totals_is_fitted_on_its_planned_values(self):
        records = parse(
            record(operation="watcher_soak", metrics=((RUN_METRIC, None, 400.0),), values=planned()),
            record(
                hl.FAILING_PODS,
                100,
                operation="watcher_soak",
                metrics=((RUN_METRIC, None, 6340.0),),
                values=planned(failing_pods=100),
            ),
        )

        prediction = hl.predict(records, RUN_METRIC, None, self.fleet(records, failing_pods=50))

        # 60 more events for each Pod failing at once, from 1 to 50.
        self.assertAlmostEqual(prediction.value, 400 + 60 * 49)

    def test_the_default_fleet_is_the_base_point_of_the_data(self):
        records = hl.load_dataset(REPO_ROOT / "missing.json").records

        fleet = hl.base_fleet(records)

        # 356 objects over the sample's 2 base clusters; the factors that the
        # totals do not count sit at the base records' planned values.
        self.assertEqual(fleet[hl.CLUSTERS], 2)
        self.assertEqual(fleet[hl.NODES], 2)
        self.assertEqual(fleet[hl.OBJECTS], 178)
        # The default fleet keeps the planned churn rate; the charts plot the achieved one.
        self.assertEqual(fleet[hl.CHURN], 10)
        self.assertEqual(fleet[hl.FAILING_PODS], 1)
        self.assertEqual(fleet[hl.CONCURRENT], 1)


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.records = parse(
            record(
                metrics=(memory("agent-api-auth", 500), (hl.CPU_PEAK, "agent-api-auth", 0.2)),
                limits={"agent-api-auth": {"memory_bytes": 2 * GIB, "cpu_cores": 1.0}},
            ),
            record(
                "clusters",
                8,
                metrics=(memory("agent-api-auth", 800), (hl.CPU_PEAK, "agent-api-auth", 0.3)),
                totals={"clusters": 8},
            ),
        )

    def checks(self, clusters: float, limits: dict) -> dict[str, hl.Check]:
        fleet = {**hl.base_fleet(self.records), hl.CLUSTERS: clusters}
        checks = hl.resource_checks(self.records, fleet, ["agent-api-auth"], limits)
        return {check.metric: check for check in checks}

    def test_headroom_and_status_against_the_limit(self):
        limits = hl.usual_limits(self.records)

        passing = self.checks(8, limits)[hl.MEMORY_PEAK]
        failing = self.checks(40, limits)[hl.MEMORY_PEAK]

        self.assertEqual(passing.status, hl.PASS)
        self.assertAlmostEqual(hl.headroom(passing), (2048 - 800) * MIB)
        # 500 MiB + 50 MiB per cluster x 38 clusters = 2,400 MiB.
        self.assertEqual(failing.status, hl.FAIL)
        self.assertAlmostEqual(hl.headroom(failing), (2048 - 2400) * MIB)
        self.assertAlmostEqual(failing.reached, (2048 - 500) / (2400 - 500))

    def test_the_limits_table_round_trips_in_mib_and_cores(self):
        table = hl.limit_table(hl.usual_limits(self.records), ["agent-api-auth", "other"])

        self.assertEqual(table[hl.LIMIT_MEMORY_COLUMN], [2048.0, 0.0])
        table[hl.LIMIT_CPU_COLUMN] = [None, 0.5]
        self.assertEqual(
            hl.limits_from_table(table),
            {
                "agent-api-auth": {hl.LIMIT_MEMORY: 2 * GIB, hl.LIMIT_CPU: 0.0},
                "other": {hl.LIMIT_MEMORY: 0.0, hl.LIMIT_CPU: 0.5},
            },
        )

    def test_an_edited_limit_replaces_the_measured_one(self):
        limits = {"agent-api-auth": {hl.LIMIT_MEMORY: 512 * MIB, hl.LIMIT_CPU: 1.0}}

        self.assertEqual(self.checks(8, limits)[hl.MEMORY_PEAK].status, hl.FAIL)

    def test_a_container_without_a_limit_is_reported(self):
        self.assertEqual(self.checks(8, {})[hl.CPU_PEAK].status, hl.NO_LIMIT)

    def test_a_resource_no_run_measured_has_no_check(self):
        records = parse(
            record(metrics=(memory("agent-api-auth", 500), (hl.RESTARTS, "provision", 0))),
            record("clusters", 8, metrics=(memory("agent-api-auth", 800),), totals={"clusters": 8}),
        )

        checks = hl.resource_checks(
            records, hl.base_fleet(records), ["agent-api-auth", "provision"], {}
        )

        self.assertEqual(
            [(check.container, check.metric) for check in checks],
            [("agent-api-auth", hl.MEMORY_PEAK)],
        )

    def test_the_response_gate_warns_above_30_s_and_fails_above_60_s(self):
        self.assertEqual(hl.evaluate(20, hl.RESPONSE_LINES), (hl.PASS, None))
        self.assertEqual(hl.evaluate(45, hl.RESPONSE_LINES)[0], hl.WARN)
        status, crossed = hl.evaluate(70, hl.RESPONSE_LINES)
        self.assertEqual((status, crossed.label), (hl.FAIL, "60 s: investigate"))

    def test_the_credential_proxy_wait_fails_only_at_the_60_s_broker_refusal(self):
        self.assertEqual([line.value for line in hl.PROXY_WAIT_LINES], [60.0])
        self.assertEqual(hl.evaluate(16, hl.PROXY_WAIT_LINES)[0], hl.PASS)
        self.assertEqual(hl.evaluate(61, hl.PROXY_WAIT_LINES)[0], hl.FAIL)

    def test_any_count_above_zero_fails(self):
        self.assertEqual(hl.evaluate(0, hl.ABOVE_ZERO)[0], hl.PASS)
        self.assertEqual(hl.evaluate(0.2, hl.ABOVE_ZERO)[0], hl.FAIL)

    def test_a_failed_prompt_fails_its_check(self):
        records = parse(
            record(metrics=((hl.FAILED_PROMPTS, None, 0),)),
            record(
                hl.CONCURRENT,
                15,
                metrics=((hl.FAILED_PROMPTS, None, 1),),
                values=planned(concurrent=15),
            ),
        )
        fleet = {**hl.base_fleet(records), hl.CONCURRENT: 15}

        (check,) = hl.threshold_checks(records, fleet, [])

        self.assertEqual(check.lines, hl.ABOVE_ZERO)
        self.assertEqual(check.status, hl.FAIL)
        self.assertEqual([row["Check"] for row in hl.unfixable_rows([check])], ["Failed prompts"])

    def test_reached_at_is_the_share_of_growth_that_crosses_the_threshold(self):
        base = hl.base_fleet(self.records)
        grown = hl.predict(
            self.records, hl.MEMORY_PEAK, "agent-api-auth", {**base, hl.CLUSTERS: 8}
        )
        at_base = hl.predict(self.records, hl.MEMORY_PEAK, "agent-api-auth", base)

        # 500 MiB at the base and 800 MiB at 8 clusters: 650 MiB is half way.
        self.assertAlmostEqual(hl.reached_at(grown, 650 * MIB), 0.5)
        self.assertEqual(hl.reached_at(grown, 400 * MIB), 0.0)
        self.assertIsNone(hl.reached_at(at_base, 2 * GIB))

    def test_the_first_limit_is_crossed_at_the_smallest_growth(self):
        checks = self.checks(40, hl.usual_limits(self.records))

        first = hl.first_limit(checks.values())

        # Memory crosses 2 GiB at 81% of the growth to 40 clusters; CPU only
        # crosses 1 core at 126%.
        self.assertEqual(first.metric, hl.MEMORY_PEAK)
        self.assertAlmostEqual(first.reached, (2048 - 500) / (2400 - 500))
        self.assertAlmostEqual(checks[hl.CPU_PEAK].reached, (1.0 - 0.2) / (0.2 / 6 * 19))

    def test_the_response_gate_reads_the_query_operations_only(self):
        records = parse(
            record(metrics=((hl.RESPONSE, None, 240.0),)),
            record("clusters", 8, metrics=((hl.RESPONSE, None, 720.0),), totals={"clusters": 8}),
            record(operation="fan_out", metrics=((hl.RESPONSE, None, 25.0),)),
            record(
                "clusters",
                8,
                operation="fan_out",
                metrics=((hl.RESPONSE, None, 28.0),),
                totals={"clusters": 8},
            ),
        )
        fleet = hl.base_fleet(records)

        (check,) = hl.threshold_checks(records, fleet, [])
        audit_chart = hl.metric_charts(records, "fleet_audit", hl.CLUSTERS, [], fleet)[0]
        fan_out_chart = hl.metric_charts(records, "fan_out", hl.CLUSTERS, [], fleet)[0]

        self.assertEqual((check.status, check.prediction.value), (hl.PASS, 25.0))
        self.assertEqual(check.name, "Response time (fan_out)")
        self.assertEqual(audit_chart.lines, ())
        self.assertEqual(fan_out_chart.lines, hl.RESPONSE_LINES)

    def test_the_pvc_is_checked_against_its_measured_capacity(self):
        records = parse(
            record(metrics=((hl.PVC_USED, None, 40 * GIB), (hl.PVC_CAPACITY, None, 50 * GIB))),
            record(
                "clusters",
                8,
                metrics=((hl.PVC_USED, None, 46 * GIB), (hl.PVC_CAPACITY, None, 50 * GIB)),
                totals={"clusters": 8},
            ),
        )
        fleet = {**hl.base_fleet(records), hl.CLUSTERS: 16}

        (check,) = hl.threshold_checks(records, fleet, [])

        self.assertEqual(check.lines[0].value, 50 * GIB)
        self.assertEqual(check.status, hl.FAIL)


class RecommendationTest(unittest.TestCase):
    def test_the_margin_is_thirty_percent(self):
        self.assertAlmostEqual(hl.with_margin(100), 130)

    def test_a_recommendation_is_the_peak_plus_the_margin_in_its_field(self):
        records = parse(
            record(
                metrics=(
                    memory("agent-api-auth", 440),
                    (hl.CPU_PEAK, "agent-api-auth", 0.5),
                    memory("manager", 40),
                    memory("litellm", 300),
                )
            )
        )

        items = {
            item.container: item
            for item in hl.recommendations(
                records, hl.base_fleet(records), ["agent-api-auth", "manager", "litellm"]
            )
        }

        auth = items["agent-api-auth"]
        self.assertAlmostEqual(auth.memory_bytes, 1.3 * 440 * MIB)
        self.assertAlmostEqual(auth.cpu_cores, 0.65)
        self.assertEqual(auth.field, "spec.deployment.agentAPIAuth.resources")
        self.assertIn("operator.resources", items["manager"].field)
        self.assertIsNone(items["manager"].cpu_cores)
        self.assertEqual(items["litellm"].field, hl.NO_FIELD)

    def test_quantities_round_up(self):
        self.assertEqual(hl.memory_quantity(1.3 * 440 * MIB), "572Mi")
        self.assertEqual(hl.memory_quantity(572.1 * MIB), "573Mi")
        self.assertEqual(hl.cpu_quantity(0.65), "650m")
        self.assertEqual(hl.cpu_quantity(0.6501), "651m")
        self.assertEqual(hl.storage_quantity(1.5 * GIB), "2Gi")

    def test_the_smallest_machine_holds_both_memory_and_cpu(self):
        self.assertEqual(hl.smallest_machine(5 * GIB, 1.0).name, "e2-standard-2")
        self.assertEqual(hl.smallest_machine(5 * GIB, 1.95).name, "e2-standard-4")
        self.assertEqual(hl.smallest_machine(60 * GIB, 4.0).name, "n2-highmem-16")
        self.assertIsNone(hl.smallest_machine(200 * GIB, 1.0))
        self.assertIsNone(hl.smallest_machine(1 * GIB, 16.0))

    def test_the_machine_list_runs_from_smallest_to_largest(self):
        memory_sizes = [machine.allocatable_memory_gib for machine in hl.MACHINE_TYPES]
        cpu_sizes = [machine.allocatable_cpu for machine in hl.MACHINE_TYPES]

        self.assertEqual(memory_sizes, sorted(memory_sizes))
        self.assertEqual(cpu_sizes, sorted(cpu_sizes))
        for machine in hl.MACHINE_TYPES:
            with self.subTest(machine=machine.name):
                self.assertLess(machine.allocatable_cpu, machine.vcpu)
                self.assertLess(machine.allocatable_memory_gib, machine.memory_gib)

    def test_allocatable_memory_is_the_design_table(self):
        self.assertEqual(
            {machine.name: machine.allocatable_memory_gib for machine in hl.MACHINE_TYPES},
            {
                "e2-standard-2": 6.1,
                "e2-standard-4": 13.3,
                "e2-standard-8": 28.3,
                "n2-standard-16": 58.4,
                "n2-highmem-16": 118.6,
            },
        )


class NodePlanTest(unittest.TestCase):
    def test_the_gateway_containers_are_one_pod_and_each_other_is_its_own(self):
        plan = hl.node_plan(
            [
                recommendation("platform-agent", 1000, 0.5),
                recommendation("agent-api-auth", 400, 0.1),
                recommendation("hermes-bridge", 200, 0.1),
                recommendation("fluent-bit", 50, 0.05),
                recommendation("envoy-credential-proxy", 500, 0.3),
                recommendation("a-container-not-in-the-mapping", 100, 0.1),
            ],
            {},
        )

        pods = {pod.name: pod for pod in plan.pods}
        self.assertEqual(
            pods[hl.GATEWAY_POD].containers,
            ("platform-agent", "agent-api-auth", "hermes-bridge", "fluent-bit"),
        )
        self.assertAlmostEqual(pods[hl.GATEWAY_POD].memory_bytes, 1.3 * 1650 * MIB)
        self.assertAlmostEqual(pods[hl.GATEWAY_POD].cpu_cores, 1.3 * 0.75)
        self.assertEqual(
            sorted(pods),
            sorted([hl.GATEWAY_POD, "envoy-credential-proxy", "a-container-not-in-the-mapping"]),
        )

    def test_the_machine_holds_the_largest_pod_and_the_count_holds_every_pod(self):
        # Two pods of 4 GiB and 1 CPU each, after the margin: one fits an
        # e2-standard-2 (6.1 GiB, 1.93 CPU); both need two of them.
        plan = hl.node_plan(
            [
                recommendation("platform-agent", 4096 / 1.3, 1 / 1.3),
                recommendation("envoy-credential-proxy", 4096 / 1.3, 1 / 1.3),
            ],
            {},
        )

        self.assertEqual(plan.machine.name, "e2-standard-2")
        self.assertEqual(plan.nodes, 2)
        self.assertAlmostEqual(plan.total_memory_bytes, 8 * GIB)

    def test_replicas_multiply_a_pod_and_default_to_one(self):
        items = [
            recommendation("platform-agent", 1000 / 1.3, 1.0 / 1.3),
            recommendation("litellm-container", 1000 / 1.3, 0.5 / 1.3),
        ]

        once = hl.node_plan(items, {})
        three = hl.node_plan(items, {"litellm-container": 3})

        self.assertEqual([pod.replicas for pod in once.pods], [1, 1])
        self.assertAlmostEqual(once.total_cpu_cores, 1.5)
        self.assertAlmostEqual(three.total_cpu_cores, 2.5)
        # 2.5 CPU on e2-standard-2 nodes of 1.93 allocatable CPU each.
        self.assertEqual((three.machine.name, three.nodes), ("e2-standard-2", 2))

    def test_the_node_count_takes_the_larger_of_memory_and_cpu(self):
        machine = hl.MACHINE_TYPES[0]

        self.assertEqual(hl.nodes_needed(machine, 5 * GIB, 5.0), 3)
        self.assertEqual(hl.nodes_needed(machine, 13 * GIB, 0.5), 3)
        self.assertEqual(hl.nodes_needed(machine, 0.0, 0.0), 1)

    def test_a_pod_no_machine_holds_has_no_machine_and_no_node_count(self):
        plan = hl.node_plan([recommendation("platform-agent", 120 * 1024 / 1.3, 1.0)], {})

        self.assertIsNone(plan.machine)
        self.assertIsNone(plan.nodes)
        rows = hl.machine_rows(plan)
        self.assertEqual({row["Holds the largest pod"] for row in rows}, {hl.NO_LABEL})
        self.assertEqual({row["Nodes for every pod"] for row in rows}, {hl.NO_VALUE})

    def test_a_container_without_a_recommendation_requests_nothing(self):
        plan = hl.node_plan(
            [recommendation("platform-agent", 1000, 0.5), recommendation("provision", None, None)],
            {},
        )

        self.assertEqual([pod.name for pod in plan.pods], [hl.GATEWAY_POD])

    def test_the_latest_run_that_counted_a_container_sets_its_replicas(self):
        records = parse(
            record(started="2026-10-23T01:00:00Z", replicas={"litellm-container": 3, "manager": 1}),
            record(run=2, started="2026-10-23T02:00:00Z", replicas={"litellm-container": 2}),
            record(run=3, started="2026-10-23T03:00:00Z"),
        )

        self.assertEqual(hl.latest_replicas(records), {"litellm-container": 2, "manager": 1})


class PlanTest(unittest.TestCase):
    @unittest.skipUnless(PLAN_PATH.exists(), "no Phase 1 plan in this checkout")
    def test_the_base_levels_and_factors_match_the_plan(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        plan = yaml.safe_load(PLAN_PATH.read_text())

        self.assertEqual(
            {name: float(value) for name, value in plan["base"].items()},
            dict(hl.PLAN_BASE_LEVELS),
        )
        self.assertEqual(set(plan["factors"]), set(hl.FACTORS))
        self.assertIn(hl.QUERY_OPERATIONS[0], plan["operations"])


class MetricContractTest(unittest.TestCase):
    def readme_metrics(self) -> set[str]:
        """The metric names in bench/scale/README.md, the runner's documented output."""
        section = SCALE_README.read_text().split("Metric names:", 1)[1]
        return set(re.findall(r"`([a-z0-9_]+)`", section))

    def test_every_metric_the_runner_writes_has_a_spec_and_no_other(self):
        self.assertEqual(set(hl.METRICS), self.readme_metrics())

    def test_a_metric_has_a_threshold_only_where_the_design_sets_one(self):
        self.assertEqual(
            set(hl.FIXED_LINES),
            {
                hl.PROXY_WAIT_P50,
                hl.PROXY_WAIT_MAX,
                hl.RESPONSE,
                hl.RESTARTS,
                hl.OOMKILLS,
                hl.PROXY_REFUSALS,
                hl.RUN_SLOT_REFUSALS,
                hl.TRIAGE_LOST,
                hl.FAILED_PROMPTS,
            },
        )
        self.assertEqual(dict(hl.CAPACITY_METRICS), {hl.PVC_USED: hl.PVC_CAPACITY})
        # No check reads a TPM quota: no token count has a threshold.
        tokens = {name for name in hl.METRICS if "tokens" in name}
        self.assertFalse(tokens & set(hl.FIXED_LINES))

    def test_the_newer_metrics_are_charted_where_present(self):
        newer = (
            hl.FAILED_PROMPTS,
            hl.CHURN_ACHIEVED,
            hl.WATCHER_SEEN_RATE,
            hl.RUN_TOKENS_INPUT,
            hl.PROFILES,
            hl.WATCHER_SUPPRESSED,
        )
        records = parse(
            record(metrics=tuple((name, None, 1.0) for name in newer)),
            record(
                "clusters",
                8,
                metrics=tuple((name, None, 4.0) for name in newer),
                totals={"clusters": 8},
            ),
        )

        charts = hl.metric_charts(records, "fleet_audit", hl.CLUSTERS, [], hl.base_fleet(records))

        self.assertEqual([chart.metric for chart in charts], [name for name in hl.METRICS if name in newer])
        lines = {chart.metric: chart.lines for chart in charts}
        self.assertEqual(lines.pop(hl.FAILED_PROMPTS), hl.ABOVE_ZERO)
        self.assertEqual(set(lines.values()), {()})


class ChartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.records = hl.load_dataset(REPO_ROOT / "missing.json").records
        self.containers = hl.containers(self.records)
        self.fleet = {**hl.base_fleet(self.records), hl.CLUSTERS: 64}

    def memory_chart(self) -> hl.MetricChart:
        charts = hl.metric_charts(
            self.records, "fleet_audit", hl.CLUSTERS, self.containers, self.fleet
        )
        return next(chart for chart in charts if chart.metric == hl.MEMORY_PEAK)

    def test_a_chart_has_a_series_per_container_with_its_limit(self):
        chart = self.memory_chart()

        self.assertEqual([item.name for item in chart.series], self.containers)
        self.assertEqual(chart.series[1].limit, 2 * GIB)
        self.assertTrue(all(item.fit.two_point for item in chart.series))
        self.assertEqual(chart.x_end, 64)
        self.assertEqual(chart.fleet_x, 64)

    def test_the_figure_draws_the_fit_and_the_prediction(self):
        colors = hl.container_colors(self.containers, ("#000001", "#000002"), "#999999")

        figure = harness_limit_chart(self.memory_chart(), colors)

        names = [trace.name for trace in figure.data]
        self.assertIn("agent-api-auth · two-point estimate", names)
        self.assertIn("agent-api-auth · predicted, not measured", names)
        self.assertIn("predicted, not measured", names)
        self.assertIn("agent-api-auth limit", names)
        self.assertIn("this fleet", names)
        self.assertEqual(figure.data[0].marker.color, "#000001")

    def test_colours_follow_the_container_not_the_selection(self):
        palette = ("#000001", "#000002")

        colors = hl.container_colors(["a", "b", "c"], palette, "#999999")

        self.assertEqual(colors, {None: "#000001", "a": "#000001", "b": "#000002", "c": "#999999"})

    def test_the_failing_pods_axis_is_a_count_from_the_planned_values(self):
        charts = hl.metric_charts(
            self.records, "watcher_soak", hl.FAILING_PODS, self.containers, self.fleet
        )
        chart = next(chart for chart in charts if chart.metric == hl.MEMORY_PEAK)

        self.assertEqual(chart.x_title, "Pods failing at once")
        self.assertEqual(sorted({point.x for point in chart.series[0].points}), [1.0, 100.0])
        self.assertNotIn("minute", hl.FACTORS[hl.FAILING_PODS].help)

    def test_the_commit_label_shows_the_image_when_a_run_recorded_one(self):
        image = "ghcr.io/gke-labs/kube-agents/platform-agent:" + "b" * 40
        records = parse(
            record(commit="a" * 40),
            record(commit="b" * 40, image=image),
        )

        self.assertEqual(hl.commit_label(records, "a" * 40), "aaaaaaaaaaaa · phase1 · 2026-10-23")
        self.assertEqual(
            hl.commit_label(records, "b" * 40), f"bbbbbbbbbbbb · {image} · phase1 · 2026-10-23"
        )


class HarnessLimitsPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.patches = [
            patch.dict(
                os.environ,
                {
                    "KUBE_AGENTS_ADMIN_USER": "admin@example.com",
                    "KUBE_AGENTS_GCLOUD_PROJECT": TEST_PROJECT,
                    "KUBE_AGENTS_ADMIN_CONNECTION_STATE": str(root / "connection.json"),
                },
            ),
            # No measured file, so the page shows the checked-in sample.
            patch.object(hl, "DATA_PATH", root / "harness-limits.json"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def open_page(self) -> AppTest:
        app = AppTest.from_file(APP_PATH, default_timeout=60).run()
        return app.switch_page(PAGE_PATH).run()

    def test_the_page_renders_the_sample_without_a_connection(self):
        app = self.open_page()

        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])
        self.assertEqual(app.title[0].value, "Harness limits")
        self.assertTrue(any("Sample data" in item.value for item in app.warning))
        self.assertFalse(any("Connect a project" in item.value for item in app.info))
        self.assertTrue(any("checks pass" in item.value for item in app.success))
        # The gateway pod (3,235Mi, 1,586m) fits an e2-standard-2; every pod's
        # 2,717m of CPU needs two of them.
        self.assertTrue(any("**2 × e2-standard-2**" in item.value for item in app.success))
        self.assertEqual(app.expander[-1].label, "Raw records")
        raw = app.expander[-1].dataframe[0].value
        self.assertIn("Errors", raw.columns)
        self.assertIn(PROMPT_FAILURE, set(raw["Errors"]))

    def test_the_failing_pods_input_is_a_count(self):
        app = self.open_page()

        (failing,) = [item for item in app.number_input if item.label == "Pods failing at once"]

        self.assertEqual(failing.value, 1.0)
        self.assertNotIn("minute", failing.help)

    def test_the_margin_note_claims_no_error_band(self):
        app = self.open_page()

        (note,) = [item.value for item in app.info if "Why 30%" in item.value]

        self.assertIn("The 30% is the whole margin.", note)
        self.assertIn("Phase 1 draws no error band", note)
        self.assertIn("predicted, not measured", note)

    def test_the_page_names_no_project_or_cluster(self):
        app = self.open_page()
        shown = [
            str(item.value)
            for kind in ("title", "markdown", "caption", "info", "success", "warning", "error")
            for item in getattr(app.main, kind)
        ]

        self.assertTrue(shown)
        self.assertFalse([text for text in shown if TEST_PROJECT in text])
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        target = yaml.safe_load(PLAN_PATH.read_text())["target"]
        # The host cluster, a test cluster (<prefix>NN) and the zone.
        patterns = [
            re.escape(target["host_cluster"]),
            re.escape(target["cluster_prefix"]) + r"\d",
            re.escape(target["zone"]),
        ]
        for path in (PAGE_SOURCE, MODULE_SOURCE, hl.SAMPLE_PATH):
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertFalse([pattern for pattern in patterns if re.search(pattern, text)])

    def test_a_larger_fleet_fails_and_is_predicted_not_measured(self):
        app = self.open_page()
        next(item for item in app.number_input if item.label == "Clusters").set_value(64.0)
        app = app.run()

        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])
        self.assertTrue(any("checks fail" in item.value for item in app.error))
        self.assertTrue(
            any("predicted, not measured" in item.value for item in app.warning)
        )
        self.assertTrue(any("**2 × n2-standard-16**" in item.value for item in app.success))




class LevelCountTest(unittest.TestCase):
    def test_runs_at_one_planned_point_count_as_one_level(self):
        # Three base runs and three runs at one level land at slightly different x values.
        points = [
            hl.Point(257.0, 1.0, "b1", 1, True, False, "base"),
            hl.Point(257.5, 1.1, "b2", 2, True, False, "base"),
            hl.Point(258.5, 0.9, "b3", 3, True, False, "base"),
            hl.Point(4186.0, 5.0, "o1", 1, False, False, "4000"),
            hl.Point(4187.0, 5.2, "o2", 2, False, False, "4000"),
            hl.Point(4188.0, 4.8, "o3", 3, False, False, "4000"),
        ]
        fit = hl.fit_line(points)
        self.assertIsNotNone(fit)
        self.assertEqual(fit.levels, 2)
        self.assertTrue(fit.two_point)


class NodePlanFilterTest(unittest.TestCase):
    def test_finished_containers_hold_no_node(self):
        def prediction(metric, container, value):
            return hl.Prediction(metric, container, value, "base", ())

        items = [
            hl.Recommendation(c, prediction(hl.MEMORY_PEAK, c, 2**30), prediction(hl.CPU_PEAK, c, 0.5))
            for c in ("platform-agent", "provision")
        ]
        plan = hl.node_plan(items, {})
        self.assertEqual([pod.name for pod in plan.pods], [hl.GATEWAY_POD])


class ReachedAtTest(unittest.TestCase):
    def test_negligible_growth_reaches_nothing_and_large_shares_are_capped(self):
        fields = hl.Contribution.__dataclass_fields__
        contribution = hl.Contribution(**{name: (0.001 if name == "delta" else None) for name in fields})
        tiny = hl.Prediction(hl.MEMORY_PEAK, "c", 100.0, "base", (contribution,))
        self.assertIsNone(hl.reached_at(tiny, 8192.0))
        self.assertEqual(hl.percent(56770.56), hl.BEYOND_CAP_TEXT)
        self.assertEqual(hl.percent(0.5), "50%")


class PastAtBaseTest(unittest.TestCase):
    def test_a_count_at_zero_is_not_past_at_the_base(self):
        fields = hl.Contribution.__dataclass_fields__
        grows = hl.Contribution(**{name: (5.0 if name == "delta" else None) for name in fields})
        at_zero = hl.Prediction(hl.RUN_SLOT_REFUSALS, None, 0.0, "fan_out", (grows,))
        over = hl.Prediction(hl.RESPONSE, None, 144.0, "fan_out", ())
        lines = hl.threshold_lines((), hl.RUN_SLOT_REFUSALS)
        self.assertFalse(hl.past_at_base(hl.Check(hl.RUN_SLOT_REFUSALS, None, lines, at_zero, hl.FAIL, None, 0.0)))
        response_lines = hl.threshold_lines((), hl.RESPONSE)
        self.assertTrue(hl.past_at_base(hl.Check(hl.RESPONSE, None, response_lines, over, hl.FAIL, None, 0.0)))


class ChurnAxisTest(unittest.TestCase):
    def test_churn_sits_at_the_achieved_rate(self):
        raw = {
            "key": "k", "commit": "c" * 40, "mode": "next", "phase": "p", "factor": hl.CHURN, "level": 1000,
            "operation": "watcher_soak", "run": 1, "started": "2026-10-24T00:00:00Z", "ended": "2026-10-24T01:00:00Z",
            "values": {hl.CHURN: 1000}, "totals": {}, "limits": {},
            "metrics": [{"name": hl.CHURN_ACHIEVED, "value": 260.0}],
        }
        (record,) = hl.parse_records({"schema": hl.SCHEMA_VERSION, "records": [raw]}, "test")
        self.assertEqual(hl.factor_value(record, hl.CHURN), 260.0)



class Phase2FactorTest(unittest.TestCase):
    def test_a_combined_point_is_no_axis_and_is_checked_against_the_prediction(self):
        container = "platform-agent"
        records = parse(
            record(metrics=(memory(container, 500),), values=planned()),
            record(
                "clusters",
                8,
                metrics=(memory(container, 800),),
                totals={"clusters": 8, "nodes": 16},
                values=planned(clusters=8),
            ),
            record(
                "combined",
                1,
                metrics=(memory(container, 1000),),
                totals={"clusters": 8, "nodes": 16},
                values=planned(clusters=8, objects=4000),
            ),
        )
        self.assertEqual(hl.factors(records), [hl.CLUSTERS])
        self.assertEqual(hl.unmapped_factors(records), [])
        [row] = hl.combined_rows(records, [container])
        # Objects has no single-factor runs here, so the prediction is the clusters line alone.
        self.assertEqual(row["Point"], "Clusters 8.0, Objects per cluster 4,000")
        self.assertEqual(row["Measured (largest run)"], hl.format_value(hl.MEMORY_PEAK, 1000 * MIB))
        self.assertEqual(row["Predicted"], hl.format_value(hl.MEMORY_PEAK, 800 * MIB))
        self.assertEqual(row["Measured ÷ predicted"], "1.25")

    def test_proxy_load_workers_are_charted_but_move_no_prediction(self):
        records = parse(
            record(operation="proxy_load", metrics=((hl.CPU_PEAK, "credential-proxy", 0.4),),
                   values=planned(proxy_concurrency=1)),
            record("proxy_concurrency", 8, operation="proxy_load",
                   metrics=((hl.CPU_PEAK, "credential-proxy", 1.0),), values=planned(proxy_concurrency=8)),
        )
        self.assertEqual(hl.factors(records, "proxy_load"), [hl.PROXY_CONCURRENCY])
        self.assertEqual(hl.unmapped_factors(records), [])
        self.assertEqual(hl.factor_label(hl.PROXY_CONCURRENCY), "Credential proxy load workers")
        prediction = hl.predict(records, hl.CPU_PEAK, "credential-proxy", hl.base_fleet(records))
        self.assertEqual(prediction.contributions, ())
        self.assertAlmostEqual(prediction.value, 0.4)
        points = hl.series_points(records, "proxy_load", hl.PROXY_CONCURRENCY, hl.CPU_PEAK, "credential-proxy")
        self.assertEqual([p.x for p in points], [1.0, 8.0])


class CapacityTest(unittest.TestCase):
    CONTAINER = "platform-agent"
    LIMITS = {"platform-agent": {"memory_bytes": 1000 * MIB}}

    def records(self) -> tuple[hl.Record, ...]:
        """Memory 500 MiB at 2 clusters and 800 MiB at 8: 50 MiB per cluster."""
        return parse(
            record(metrics=(memory(self.CONTAINER, 500),), values=planned()),
            record(
                "clusters",
                8,
                metrics=(memory(self.CONTAINER, 800),),
                totals={"clusters": 8, "nodes": 16},
                values=planned(clusters=8),
            ),
        )

    def test_a_fleet_of_more_clusters_keeps_their_shape(self):
        fleet = hl.scaled_fleet(planned(), 8)
        self.assertEqual(fleet[hl.CLUSTERS], 8)
        self.assertEqual(fleet[hl.CHURN], 40.0)
        self.assertEqual(fleet[hl.FAILING_PODS], 4.0)
        self.assertEqual(fleet[hl.OBJECTS], planned()[hl.OBJECTS])

    def test_the_breaking_point_is_where_the_line_meets_the_limit(self):
        [memory_check] = [
            item
            for item in hl.cluster_capacities(self.records(), [self.CONTAINER], self.LIMITS)
            if item.check.metric == hl.MEMORY_PEAK
        ]
        self.assertFalse(memory_check.at_base)
        self.assertAlmostEqual(memory_check.value, 12.0, places=3)

    def test_a_limit_below_the_base_fails_at_the_base(self):
        limits = {"platform-agent": {"memory_bytes": 400 * MIB}}
        [memory_check] = hl.cluster_capacities(self.records(), [self.CONTAINER], limits)
        self.assertTrue(memory_check.at_base)

    def test_a_flat_metric_never_fails(self):
        limits = {"platform-agent": {"memory_bytes": 100_000 * MIB}}
        [memory_check] = hl.factor_capacities(self.records(), [self.CONTAINER], limits, hl.NODES)
        self.assertIsNone(memory_check.value)

    def test_a_count_onset_is_the_measured_step(self):
        refusals = hl.RUN_SLOT_REFUSALS
        records = parse(
            record(operation="fan_out", metrics=((refusals, None, 0),), values=planned()),
            *(
                record("concurrent", level, "fan_out", run, metrics=((refusals, None, count),),
                       values=planned(concurrent=level))
                for level, run, count in ((10, 1, 0), (10, 2, 0), (12, 1, 0), (12, 2, 2), (15, 1, 5))
            ),
        )
        [onset] = hl.count_onsets(records, hl.CONCURRENT)
        self.assertEqual((onset.last_zero, onset.first_above), (10.0, 12.0))
        self.assertEqual((onset.runs_above, onset.runs), (1, 2))


class ProxyCapacityTest(unittest.TestCase):
    def test_load_levels_average_their_runs(self):
        records = parse(
            *(
                record("proxy_concurrency", workers, "proxy_load", run,
                       metrics=((hl.PROXY_LOAD_RATE, None, rate), (hl.PROXY_LOAD_P50, None, 1.0 * workers),
                                (hl.CPU_PEAK, hl.PROXY_CONTAINER, cpu)),
                       values=planned(proxy_concurrency=workers))
                for workers, run, rate, cpu in ((4, 1, 2.0, 0.6), (4, 2, 3.0, 0.7), (8, 1, 3.5, 1.0))
            ),
        )
        levels = hl.proxy_levels(records)
        self.assertEqual([(level.workers, level.runs, level.per_second) for level in levels],
                         [(4.0, 2, 2.5), (8.0, 1, 3.5)])
        self.assertEqual(levels[0].cpu_cores, 0.7)

    def test_demand_is_commands_per_minute_per_cluster(self):
        records = parse(
            record("clusters", 8, "watcher_soak", metrics=((hl.PROXY_COMMANDS, None, 240.0),
                                                         (hl.PROXY_COMMAND_SECONDS, None, 480.0)),
                   totals={"clusters": 8}, started="2026-10-23T01:00:00Z"),
        )
        [demand] = hl.proxy_demand(records)
        self.assertEqual(demand.commands_per_minute, 8.0)
        self.assertEqual(demand.per_cluster_per_minute, 1.0)
        self.assertEqual(demand.seconds_per_command, 2.0)

    def test_points_at_one_cluster_count_are_kept_apart(self):
        # Ten concurrent questions on two clusters say nothing about what one cluster asks.
        records = parse(
            record(operation="fan_out", metrics=((hl.PROXY_COMMANDS, None, 60.0),), totals={"clusters": 2}),
            *(
                record("concurrent", 10, "fan_out", run, metrics=((hl.PROXY_COMMANDS, None, 600.0),),
                       totals={"clusters": 2})
                for run in (1, 2)
            ),
        )
        base, concurrent = hl.proxy_demand(records)
        self.assertEqual((base.factor, base.commands_per_minute, base.per_cluster_per_minute), ("base", 2.0, 1.0))
        self.assertEqual((concurrent.factor, concurrent.runs, concurrent.commands_per_minute), ("concurrent", 2, 20.0))
        self.assertIsNone(concurrent.per_cluster_per_minute)


class CappedCpuTest(unittest.TestCase):
    def test_a_cpu_peak_at_the_limit_stays_out_of_the_line(self):
        proxy, limits = "envoy-credential-proxy", {"envoy-credential-proxy": {"cpu_cores": 1.0}}
        records = parse(
            record(metrics=((hl.CPU_PEAK, proxy, 0.4),), limits=limits, values=planned()),
            record("clusters", 4, metrics=((hl.CPU_PEAK, proxy, 0.6),), limits=limits,
                   totals={"clusters": 4}, values=planned(clusters=4)),
            # Pinned at the 1-core limit: the need is unknown, so it is not a point of the line.
            record("clusters", 16, metrics=((hl.CPU_PEAK, proxy, 1.0),), limits=limits,
                   totals={"clusters": 16}, values=planned(clusters=16)),
        )
        points = hl.series_points(records, "fleet_audit", hl.CLUSTERS, hl.CPU_PEAK, proxy)
        self.assertEqual([p.excluded for p in points], [False, False, True])
        fit = hl.fit_line(points)
        # 0.4 at 2 clusters and 0.6 at 4: 0.1 per cluster, so 1.8 cores of need at 16.
        self.assertAlmostEqual(fit.at(16), 1.8)

    def test_cpu_runs_group_by_operation_fleet_and_limit(self):
        proxy = "envoy-credential-proxy"
        records = parse(
            *(
                record("clusters", 16, "audits_30m", run, totals={"clusters": 16},
                       limits={proxy: {"cpu_cores": limit}},
                       metrics=((hl.CPU_PEAK, proxy, peak), (hl.CPU_SECONDS, proxy, seconds)))
                for run, limit, peak, seconds in ((1, 1.0, 1.0, 1440.0), (2, 1.0, 0.8, 1080.0), (3, 4.0, 2.2, 3600.0))
            ),
        )
        default, raised = hl.cpu_runs(records, proxy)
        self.assertEqual((default.limit, default.runs, default.peak, default.capped), (1.0, 2, 1.0, True))
        # 1,440 and 1,080 CPU-seconds over 30-minute windows: 0.8 and 0.6 cores on average.
        self.assertAlmostEqual(default.average, 0.7)
        self.assertEqual((raised.limit, raised.peak, raised.capped), (4.0, 2.2, False))


class UnanchoredOperationTest(unittest.TestCase):
    def test_an_operation_without_base_runs_anchors_nothing(self):
        container = "envoy-credential-proxy"
        records = parse(
            record(metrics=((hl.CPU_PEAK, container, 0.6),), values=planned()),
            # Measured once, at 16 clusters under raised limits: no base point of its own.
            record("clusters", 16, "audits_raised", metrics=((hl.CPU_PEAK, container, 2.5),),
                   totals={"clusters": 16}, values=planned(clusters=16)),
        )
        self.assertEqual(hl.base_fleet(records)[hl.CLUSTERS], 2.0)
        self.assertAlmostEqual(hl.predict(records, hl.CPU_PEAK, container, hl.base_fleet(records)).base, 0.6)


class UsualLimitsTest(unittest.TestCase):
    def test_a_short_experiment_under_raised_limits_does_not_change_the_limits(self):
        default, raised = {"cpu_cores": 1.0, "memory_bytes": 2 * GIB}, {"cpu_cores": 4.0, "memory_bytes": 4 * GIB}
        records = parse(
            record(run=1, limits={"envoy-credential-proxy": default}, started="2026-10-27T01:00:00Z"),
            record(run=2, limits={"envoy-credential-proxy": default}, started="2026-10-27T02:00:00Z"),
            record("clusters", 16, "audits_raised", limits={"envoy-credential-proxy": raised},
                   started="2026-10-27T03:00:00Z"),
        )
        self.assertEqual(hl.usual_limits(records)["envoy-credential-proxy"], default)


if __name__ == "__main__":
    unittest.main()
