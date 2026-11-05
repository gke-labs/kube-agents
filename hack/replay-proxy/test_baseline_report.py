"""The baseline report over synthetic shadow lines and eval data."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import baseline_report as br

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = {
    "as_of": "2026-11-04",
    "source": "test",
    "models": {
        "m1": {"input": 2.0, "cached_input": 0.2, "output": 12.0,
               "long_context": {"above_prompt_tokens": 200000, "input": 4.0, "cached_input": 0.4, "output": 18.0}},
        "m2": {"input": 1.0, "cached_input": 0.1, "output": 5.0},
    },
}


def prices(price_as=None, table=None):
    return br.Prices(table or TABLE, "digest", price_as or {})


def line(request_id, ts, kind="nightly", build="b1", status=200, prompt=1000, cached=0, completion=10,
         latency_ms=1000, estimated=False, group="m1", fallbacks="0", project="p1", pr="", **extra):
    entry = {
        "event": "replay_shadow_request",
        "request_id": request_id,
        "ts": ts,
        "job": {"kind": kind, "project": project, "build": build, "pr": pr},
        "model": "model-default",
        "status": status,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "completion_tokens": completion,
        "tokens_estimated": estimated,
        "litellm_model_group": group,
        "litellm_attempted_fallbacks": fallbacks,
        "upstream_incomplete": False,
        "fingerprint": "f" + request_id,
    }
    entry.update(extra)
    return entry


def error_line(ts, error, kind="nightly", build="b1", status=200):
    return {"event": "replay_shadow_request", "schema": 1, "ts": ts,
            "job": {"kind": kind, "project": "p1", "build": build, "pr": ""}, "status": status, "error": error}


def eval_run(build_id, tasks, tier="nightly"):
    return {"build_id": build_id, "tier": tier, "tasks": tasks}


def task(name, *reps):
    return {"name": name, "result": "pass", "reps": [dict(n=i + 1, **rep) for i, rep in enumerate(reps)]}


TEMP_DIR = None


def setUpModule():
    global TEMP_DIR
    TEMP_DIR = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


def write(lines, suffix=".jsonl", directory=None):
    handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, dir=directory or TEMP_DIR)
    with handle:
        for entry in lines:
            handle.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")
    return handle.name


def write_json(doc, name=None, directory=None):
    path = os.path.join(directory or TEMP_DIR, name) if name else tempfile.mktemp(suffix=".json", dir=TEMP_DIR)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    return path


def report(lines, runs=None, price_as=None, since=None, until=None):
    quality = br.Quality()
    loaded, manifests = br.load_lines([write(lines)], quality)
    loaded = br.in_window(loaded, since, until, quality)
    eval_runs = br.load_eval([write_json({"runs": runs})], quality) if runs is not None else {}
    return br.build_report(loaded, manifests, eval_runs, prices(price_as), quality, since, until)


def run_main(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = br.main(argv)
    return code, out.getvalue(), err.getvalue()


class TokenMetrics(unittest.TestCase):
    def test_tokens_split_cached_and_uncached_over_ok_lines_only(self):
        r = report([
            line("a", 1, prompt=1000, cached=600, completion=10),
            line("b", 2, prompt=500, cached=0, completion=20),
            line("c", 3, status=429, prompt=9999, cached=None, completion=None, estimated=True),
        ])
        total = r["scopes"]["total"]
        self.assertEqual(total["requests"], 3)
        self.assertEqual(total["ok_requests"], 2)
        self.assertEqual(total["prompt_tokens"], 1500)
        self.assertEqual(total["cached_prompt_tokens"], 600)
        self.assertEqual(total["uncached_prompt_tokens"], 900)
        self.assertEqual(total["completion_tokens"], 30)

    def test_cached_above_prompt_is_capped_at_prompt(self):
        total = report([line("a", 1, prompt=100, cached=150)])["scopes"]["total"]
        self.assertEqual((total["cached_prompt_tokens"], total["uncached_prompt_tokens"]), (100, 0))

    def test_cache_hit_rate_is_token_weighted_and_leaves_out_estimated_lines(self):
        total = report([
            line("a", 1, prompt=1000, cached=900),
            line("b", 2, prompt=3000, cached=100),
            line("c", 3, prompt=6000, cached=None, estimated=True),
        ])["scopes"]["total"]
        self.assertEqual(total["cache_hit_rate"], 0.25)
        self.assertEqual(total["estimated_prompt_token_share"], 0.6)

    def test_rates_are_null_with_no_tokens(self):
        total = report([line("a", 1, status=500, prompt=None, cached=None)])["scopes"]["total"]
        self.assertIsNone(total["cache_hit_rate"])
        self.assertIsNone(total["estimated_prompt_token_share"])

    def test_per_kind_per_build_and_per_build_ratios(self):
        r = report([
            line("a", 1, kind="nightly", build="n1", prompt=100),
            line("b", 2, kind="presubmit", build="p1", pr="7", prompt=200),
            line("c", 3, kind="presubmit", build="p2", pr="7", prompt=400),
            line("d", 4, kind="presubmit", build="p2", pr="7", prompt=600),
        ])
        self.assertEqual(r["scopes"]["nightly"]["prompt_tokens"], 100)
        self.assertEqual(r["scopes"]["presubmit"]["builds"], 2)
        self.assertEqual(r["scopes"]["presubmit"]["prompt_tokens_per_build"], 600.0)
        self.assertEqual(r["scopes"]["presubmit"]["requests_per_build"], 1.5)
        self.assertEqual(r["scopes"]["total"]["builds"], 3)
        builds = {b["build"]: b for b in r["builds"]}
        self.assertEqual(builds["p2"]["metrics"]["prompt_tokens"], 1000)
        self.assertEqual((builds["p2"]["kind"], builds["p2"]["pr"]), ("presubmit", "7"))

    def test_error_and_dropped_lines_are_requests_without_tokens(self):
        r = report([line("a", 1), error_line(2, "analysis_dropped", status=429), error_line(3, "KeyError")])
        total = r["scopes"]["total"]
        self.assertEqual(total["requests"], 3)
        self.assertEqual(total["prompt_tokens"], 1000)
        self.assertEqual(total["rate_limited_requests"], 1)
        self.assertEqual(r["data_quality"]["analysis_dropped_lines"], 1)
        self.assertEqual(r["data_quality"]["analysis_error_lines"], 1)


class TimeAndLimitMetrics(unittest.TestCase):
    def test_model_time_and_nearest_rank_percentiles(self):
        lines = [line(str(i), i, latency_ms=ms) for i, ms in enumerate([100, 200, 300, 400, 10000])]
        lines.append(line("x", 9, status=502, latency_ms=None, prompt=None))
        total = report(lines)["scopes"]["total"]
        self.assertEqual(total["model_time_s"], 11.0)
        self.assertEqual(total["model_time_per_build_s"], 11.0)
        self.assertEqual(total["latency_p50_ms"], 300)
        self.assertEqual(total["latency_p95_ms"], 10000)

    def test_failed_requests_count_toward_model_time(self):
        total = report([line("a", 1, latency_ms=1000), line("b", 2, status=429, latency_ms=3000)])["scopes"]["total"]
        self.assertEqual(total["model_time_s"], 4.0)

    def test_percentile(self):
        self.assertIsNone(br.percentile([], 50))
        self.assertEqual(br.percentile([5], 95), 5)
        self.assertEqual(br.percentile(list(range(1, 21)), 95), 19)
        self.assertEqual(br.percentile(list(range(1, 21)), 50), 10)

    def test_statuses_rate_limits_fallbacks_and_incomplete_answers(self):
        total = report([
            line("a", 1),
            line("b", 2, status=429, fallbacks=None),
            line("c", 3, status=429, fallbacks=None),
            line("d", 4, status=500, fallbacks=None),
            line("e", 5, fallbacks="1"),
            line("f", 6, fallbacks=None),
            line("g", 7, upstream_incomplete=True),
            line("h", 8, status="429"),
        ])["scopes"]["total"]
        self.assertEqual(total["non_200_requests"], 4)
        self.assertEqual(total["rate_limited_requests"], 2)
        self.assertEqual(total["rate_limited_rate"], 0.25)
        self.assertEqual(total["status_counts"], {"429": 2, "500": 1, "none": 1})
        self.assertEqual(total["fallback_attempted_requests"], 1)
        self.assertEqual(total["upstream_incomplete_requests"], 1)


class PriceTable(unittest.TestCase):
    def test_cost_prices_uncached_cached_and_output(self):
        total = report([line("a", 1, prompt=100_000, cached=40_000, completion=1_000)])["scopes"]["total"]
        # 60k x $2 + 40k x $0.20 + 1k x $12, per 1M.
        self.assertAlmostEqual(total["cost_usd"], 0.12 + 0.008 + 0.012)
        self.assertAlmostEqual(total["cost_per_build_usd"], 0.14)

    def test_cost_per_build_divides_by_builds(self):
        total = report([line("a", 1, build="b1", group="m2", prompt=1_000_000, completion=0),
                        line("b", 2, build="b2", group="m2", prompt=1_000_000, completion=0)])["scopes"]["total"]
        self.assertAlmostEqual(total["cost_usd"], 2.0)
        self.assertAlmostEqual(total["cost_per_build_usd"], 1.0)

    def test_long_context_price_above_the_threshold(self):
        total = report([line("a", 1, prompt=300_000, cached=100_000, completion=1_000)])["scopes"]["total"]
        self.assertAlmostEqual(total["cost_usd"], 0.8 + 0.04 + 0.018)

    def test_at_the_threshold_is_not_long_context(self):
        total = report([line("a", 1, prompt=200_000, cached=0, completion=0)])["scopes"]["total"]
        self.assertAlmostEqual(total["cost_usd"], 0.4)

    def test_an_unknown_model_is_reported_never_zero(self):
        r = report([line("a", 1, group="mystery", prompt=1_000_000, completion=0), line("b", 2, group="m2",
                                                                                        prompt=1_000_000)])
        total = r["scopes"]["total"]
        self.assertEqual(total["unpriced_requests"], 1)
        self.assertEqual(total["unpriced"], {"mystery": 1})
        self.assertAlmostEqual(total["cost_usd"], 1.0 + 10 * 5.0 / 1_000_000)
        self.assertIn("mystery", br.render(r))

    def test_a_line_without_a_model_is_unpriced_under_a_label(self):
        p = prices()
        model, label = p.model_for(br.Line("nightly", "b", "", "", 0, 200, 0, 1, 0, 0, False, None, None,
                                           False, False, None))
        self.assertEqual((model, label), (None, br.UNPRICED_NO_MODEL))

    def test_price_as_maps_the_alias_to_a_priced_model(self):
        r = report([line("a", 1, group="model-default", prompt=1_000_000, completion=0)],
                   price_as={"model-default": "m2"})
        self.assertEqual(r["scopes"]["total"]["unpriced_requests"], 0)
        self.assertAlmostEqual(r["scopes"]["total"]["cost_usd"], 1.0)
        self.assertEqual(r["prices"]["price_as"], {"model-default": "m2"})

    def test_the_model_field_is_the_fallback_when_no_group_was_logged(self):
        r = report([line("a", 1, group=None, prompt=1_000_000, completion=0)], price_as={"model-default": "m2"})
        self.assertAlmostEqual(r["scopes"]["total"]["cost_usd"], 1.0)

    def test_price_as_to_a_model_the_table_lacks_is_refused(self):
        with self.assertRaises(ValueError):
            prices({"model-default": "nope"})

    def test_a_malformed_table_is_refused(self):
        for table in ({"models": {}}, {"as_of": "x", "models": {"m": {"input": 1, "output": 1}}},
                      {"as_of": "x", "models": {"m": {"input": "1", "cached_input": 0, "output": 1}}},
                      {"as_of": "x", "models": {"m": {"input": 1, "cached_input": 0, "output": 1,
                                                      "long_context": {"input": 1, "cached_input": 0, "output": 1}}}}):
            with self.assertRaises(ValueError, msg=table):
                br.Prices(table, "d", {})

    def test_the_checked_in_table_loads_and_is_dated(self):
        loaded = br.Prices.load(br.DEFAULT_PRICES_PATH, {})
        self.assertRegex(loaded.as_of, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(loaded.models)
        self.assertEqual(len(loaded.digest), 64)

    def test_parse_price_as(self):
        self.assertEqual(br.parse_price_as(["a=b", "c=d=e"]), {"a": "b", "c": "d=e"})
        for bad in ("a", "=b", "a="):
            with self.assertRaises(ValueError):
                br.parse_price_as([bad])


class EvalJoin(unittest.TestCase):
    RUNS = [
        eval_run("b1", [
            task("case-a", {"result": "pass", "agent_s": 100.0, "tokens": {"total": 5000}},
                 {"result": "fail", "agent_s": 300.0}, {"result": "infra"}),
            task("case-b", {"result": "pass", "agent_s": 50.0}, {"result": "pass", "agent_s": 50.0}),
        ]),
        eval_run("b2", [{"name": "case-a", "result": "fail", "duration_s": 99}], tier="presubmit"),
        eval_run("not-logged", [task("case-z", {"result": "pass", "agent_s": 1.0})]),
    ]

    def test_joins_on_build_id_and_relates_model_time_to_agent_time(self):
        r = report([line("a", 1, build="b1", latency_ms=100_000), line("b", 2, build="b1", latency_ms=25_000),
                    line("c", 3, build="b3", latency_ms=1_000_000)], runs=self.RUNS)
        total = r["scopes"]["total"]
        self.assertEqual(total["eval_builds_joined"], 1)
        self.assertEqual(total["agent_time_s"], 500.0)
        # b3 has no eval run, so its model time stays out of the share.
        self.assertEqual(total["model_time_share_of_agent_time"], 0.25)
        self.assertEqual(total["harness_tokens_total"], 5000)
        self.assertEqual(r["data_quality"]["builds_without_eval_run"], 1)

    def test_pass_rate_leaves_out_infra_and_flaky_cases_disagree_within_a_build(self):
        total = report([line("a", 1, build="b1")], runs=self.RUNS)["scopes"]["total"]
        self.assertEqual((total["graded_reps"], total["passed_reps"], total["infra_reps"]), (4, 3, 1))
        self.assertEqual(total["pass_rate"], 0.75)
        self.assertEqual((total["cases"], total["flaky_cases"]), (2, 1))

    def test_disagreement_across_builds_is_not_flaky(self):
        runs = [eval_run("b1", [task("c", {"result": "pass"})]), eval_run("b2", [task("c", {"result": "fail"})])]
        r = report([line("a", 1, build="b1"), line("b", 2, build="b2")], runs=runs)
        self.assertEqual(r["scopes"]["total"]["flaky_cases"], 0)
        self.assertEqual(r["cases"]["nightly"]["c"]["case_pass_rate"], 0.5)

    def test_case_table_per_kind(self):
        r = report([line("a", 1, build="b1"), line("b", 2, kind="presubmit", build="b2", pr="3")], runs=self.RUNS)
        nightly = r["cases"]["nightly"]
        self.assertEqual(set(nightly), {"case-a", "case-b"})
        self.assertEqual(nightly["case-a"], {
            "case_builds": 1, "case_graded_reps": 2, "case_passed_reps": 1, "case_infra_reps": 1,
            "case_pass_rate": 0.5, "case_flaky_builds": 1, "case_agent_s_p50": 100.0,
        })
        # A task with no repetitions counts its verdict once, with no timing.
        self.assertEqual(r["cases"]["presubmit"]["case-a"]["case_graded_reps"], 1)
        self.assertIsNone(r["cases"]["presubmit"]["case-a"]["case_agent_s_p50"])
        self.assertNotIn("case-z", nightly)

    def test_without_eval_data_the_eval_metrics_are_null(self):
        r = report([line("a", 1)])
        for name in ("agent_time_s", "model_time_share_of_agent_time", "pass_rate", "flaky_cases", "graded_reps"):
            self.assertIsNone(r["scopes"]["total"][name], name)
        self.assertEqual(r["scopes"]["total"]["eval_builds_joined"], 0)
        self.assertIsNone(r["data_quality"]["builds_without_eval_run"])

    def test_a_joined_build_without_timing_has_no_share(self):
        runs = [eval_run("b1", [task("c", {"result": "pass"})])]
        total = report([line("a", 1, build="b1")], runs=runs)["scopes"]["total"]
        self.assertIsNone(total["model_time_share_of_agent_time"])
        self.assertEqual(total["pass_rate"], 1.0)

    def test_tier_mismatch_duplicates_and_unknown_results_are_noted(self):
        runs = [eval_run("b1", [task("c", {"result": "pass"}, {"result": "blocked"})], tier="presubmit"),
                eval_run("b1", [task("c", {"result": "fail"})])]
        r = report([line("a", 1, build="b1")], runs=runs)
        quality = r["data_quality"]
        self.assertEqual((quality["eval_tier_mismatches"], quality["eval_duplicate_runs"],
                          quality["eval_unknown_results"]), (1, 1, 1))
        self.assertEqual(r["scopes"]["total"]["pass_rate"], 1.0)

    def test_a_gitlab_run_joins_a_presubmit_without_a_mismatch(self):
        runs = [eval_run("b1", [task("c", {"result": "pass"})], tier="gitlab")]
        r = report([line("a", 1, kind="presubmit", build="b1", pr="2")], runs=runs)
        self.assertEqual(r["data_quality"]["eval_tier_mismatches"], 0)

    def test_eval_data_without_runs_is_refused(self):
        with self.assertRaises(ValueError):
            br.load_eval([write_json({"cases": []})], br.Quality())


class Compare(unittest.TestCase):
    def reports(self):
        baseline = report([line("a", 1, prompt=1_000_000, cached=500_000, completion=0, latency_ms=4000),
                           line("b", 2, status=429)])
        current = report([line("a", 1, prompt=500_000, cached=400_000, completion=0, latency_ms=1000)])
        return baseline, current

    def test_deltas_absolute_and_percent(self):
        baseline, current = self.reports()
        rows = br.compare(baseline, current)["scopes"]["total"]
        # The 429 line bills nothing, so its estimated prompt stays out.
        self.assertEqual(rows["prompt_tokens"], {"baseline": 1_000_000, "current": 500_000, "delta": -500_000,
                                                 "delta_percent": -50.0})
        self.assertEqual(rows["cache_hit_rate"], {"baseline": 0.5, "current": 0.8, "delta": 0.3,
                                                  "delta_percent": 60.0})
        self.assertEqual(rows["model_time_s"]["delta"], -4.0)
        self.assertEqual(rows["rate_limited_requests"]["delta_percent"], -100.0)
        self.assertEqual(set(rows), set(br.HEADLINE_METRICS))

    def test_a_zero_or_missing_baseline_has_no_percent(self):
        baseline, current = self.reports()
        rows = br.compare(current, baseline)["scopes"]
        self.assertEqual(rows["total"]["rate_limited_requests"]["delta"], 1)
        self.assertIsNone(rows["total"]["rate_limited_requests"]["delta_percent"])
        self.assertIsNone(rows["presubmit"]["cache_hit_rate"]["delta"])

    def test_notes_name_what_makes_the_comparison_unequal(self):
        baseline, current = self.reports()
        self.assertEqual(br.compare(baseline, current)["notes"], [])
        current["prices"]["sha256"] = "other"
        current["report_schema"] = br.REPORT_SCHEMA_VERSION + 1
        current["scopes"]["total"]["unpriced_requests"] = 3
        notes = " | ".join(br.compare(baseline, current)["notes"])
        self.assertIn("price tables", notes)
        self.assertIn("report schemas differ", notes)
        self.assertIn("current total: cost is partial", notes)

    def test_rendered_rates_are_percentage_points(self):
        baseline, current = self.reports()
        text = br.render_compare(br.compare(baseline, current))
        self.assertRegex(text, r"cache_hit_rate .* pp")
        self.assertIn("-50.0%", text)


class LiteLLMAccounting(unittest.TestCase):
    def test_litellm_cost_is_preferred_and_the_table_is_the_fallback(self):
        r = report([
            line("a", 1, group="m2", prompt=1_000_000, completion=0, litellm_cost_usd=0.5),
            line("b", 2, group="m2", prompt=1_000_000, completion=0),
            line("c", 3, group="mystery", prompt=1_000_000, completion=0, litellm_cost_usd=0.25),
            line("d", 4, group="mystery"),
            line("e", 5, status=429, group="m2", litellm_cost_usd=9.0),
        ])
        total = r["scopes"]["total"]
        self.assertAlmostEqual(total["cost_usd"], 0.5 + 1.0 + 0.25)
        self.assertAlmostEqual(total["litellm_cost_usd"], 0.75)
        self.assertEqual(total["litellm_costed_requests"], 2)
        self.assertAlmostEqual(total["table_cost_usd"], 2.0)
        self.assertEqual(total["table_priced_requests"], 2)
        self.assertEqual(total["cost_from_table_requests"], 1)
        # Over line a only, the one with both: $1.00 by the table against $0.50 by LiteLLM.
        self.assertEqual(total["table_to_litellm_cost_ratio"], 2.0)
        self.assertEqual((total["unpriced_requests"], total["unpriced"]), (1, {"mystery": 1}))

    def test_no_litellm_cost_means_no_ratio(self):
        total = report([line("a", 1, group="m2")])["scopes"]["total"]
        self.assertIsNone(total["table_to_litellm_cost_ratio"])
        self.assertEqual(total["litellm_costed_requests"], 0)

    def test_the_deployments_model_is_priced_before_the_alias_and_price_as(self):
        r = report([line("a", 1, group="model-default", litellm_model_name="vertex_ai/m2", prompt=1_000_000,
                         completion=0)], price_as={"model-default": "m1"})
        self.assertAlmostEqual(r["scopes"]["total"]["cost_usd"], 1.0)

    def test_a_model_name_without_a_prefix_matches_as_it_is(self):
        self.assertEqual(br.strip_provider("m2"), "m2")
        self.assertEqual(br.strip_provider("hosted_vllm/org/m"), "org/m")
        self.assertIsNone(br.strip_provider(None))
        r = report([line("a", 1, group="nope", litellm_model_name="m2", prompt=1_000_000, completion=0)])
        self.assertAlmostEqual(r["scopes"]["total"]["cost_usd"], 1.0)

    def test_price_as_is_the_fallback_and_unpriced_lines_carry_the_model_name(self):
        r = report([line("a", 1, group="model-default", litellm_model_name="vertex_ai/new", prompt=1_000_000,
                         completion=0)], price_as={"model-default": "m2"})
        self.assertAlmostEqual(r["scopes"]["total"]["cost_usd"], 1.0)
        # Even a --price-as naming the deployment's model loses to the table's entry for it.
        r = report([line("a", 1, group="model-default", litellm_model_name="vertex_ai/m2", prompt=1_000_000,
                         completion=0)], price_as={"vertex_ai/m2": "m1"})
        self.assertAlmostEqual(r["scopes"]["total"]["cost_usd"], 1.0)
        r = report([line("a", 1, group="model-default", litellm_model_name="vertex_ai/new")])
        self.assertEqual(r["scopes"]["total"]["unpriced"], {"new": 1})

    def test_reasoning_and_cache_write_tokens(self):
        total = report([
            line("a", 1, completion=100, reasoning_tokens=80, cache_write_tokens=30),
            line("b", 2, completion=100, reasoning_tokens=20),
            line("c", 3, completion=1000),
            line("d", 4, status=429, reasoning_tokens=500, cache_write_tokens=500),
        ])["scopes"]["total"]
        self.assertEqual(total["reasoning_tokens"], 100)
        # Line c logs no reasoning count, so it stays out of the share.
        self.assertEqual(total["reasoning_token_share"], 0.5)
        self.assertEqual(total["cache_write_tokens"], 30)

    def test_litellm_retries(self):
        r = report([
            line("a", 1, litellm_attempted_retries=2),
            line("b", 2, status=429, litellm_attempted_retries=1),
            line("c", 3, litellm_attempted_retries=0),
            line("d", 4),
            line("e", 5, litellm_attempted_retries="two", litellm_cost_usd="free"),
            line("f", 6, litellm_cost_usd=-1.0),
        ])
        total = r["scopes"]["total"]
        self.assertEqual((total["retried_requests"], total["litellm_retries"]), (2, 3))
        self.assertEqual(total["rate_limited_requests"], 1)
        self.assertEqual(r["data_quality"]["bad_field_values"],
                         {"litellm_attempted_retries": 1, "litellm_cost_usd": 2})
        self.assertEqual(total["litellm_costed_requests"], 0)

    def test_compare_notes_mixed_and_changed_cost_sources(self):
        table_only = report([line("a", 1, group="m2")])
        mixed = report([line("a", 1, group="m2"), line("b", 2, group="m2", litellm_cost_usd=0.1)])
        litellm_only = report([line("a", 1, group="m2", litellm_cost_usd=0.1)])
        self.assertEqual(br.compare(table_only, table_only)["notes"], [])
        notes = " | ".join(br.compare(table_only, litellm_only)["notes"])
        self.assertIn("total: cost_usd comes from price table in the baseline and litellm now", notes)
        notes = " | ".join(br.compare(mixed, litellm_only)["notes"])
        self.assertIn("baseline total: cost_usd takes LiteLLM's cost for 1 requests and the price table for 1", notes)
        for name in ("reasoning_tokens", "retried_requests", "litellm_retries"):
            self.assertIn(name, br.HEADLINE_METRICS)

    def test_a_line_written_before_the_fields_existed_reads_as_before(self):
        total = report([line("a", 1, group="m2", prompt=1_000_000, completion=0)])["scopes"]["total"]
        self.assertEqual((total["reasoning_tokens"], total["cache_write_tokens"], total["litellm_retries"]), (0, 0, 0))
        self.assertIsNone(total["reasoning_token_share"])
        self.assertAlmostEqual(total["cost_usd"], 1.0)


class MalformedInput(unittest.TestCase):
    def test_skips_and_bad_values_are_counted_not_fatal(self):
        r = report([
            "not json",
            json.dumps({"event": "replay_shadow_start"}),
            json.dumps([1, 2]),
            line("a", 1),
            line("a", 1),
            line("b", 2, kind="adhoc"),
            dict(line("c", 3), job="nightly"),
            line("d", "yesterday"),
            line("e", True),
            line("f", 4, prompt=True, cached=-5, completion="7", latency_ms=1.5),
        ])
        quality = r["data_quality"]
        self.assertEqual(quality["skipped_lines"], {
            "not_json": 1, "other_event": 2, "duplicate_request_id": 1, "untagged": 2, "bad_ts": 2,
        })
        self.assertEqual(quality["bad_field_values"],
                         {"prompt_tokens": 1, "cached_tokens": 1, "completion_tokens": 1, "latency_ms": 1})
        total = r["scopes"]["total"]
        self.assertEqual(total["requests"], 2)
        self.assertEqual(total["prompt_tokens"], 1000)
        self.assertEqual(r["data_quality"]["lines_without_latency"], 1)

    def test_directories_yield_segments_and_manifests_only(self):
        root = tempfile.mkdtemp(dir=TEMP_DIR)
        build = os.path.join(root, "nightly", "2026-11-05", "b1")
        os.makedirs(os.path.join(build, "logs"))
        os.makedirs(os.path.join(build, "store", "blobs"))
        write([line("a", 1)], directory=os.path.join(build, "logs"))
        write([line("b", 2)], suffix=".jsonl.open", directory=os.path.join(build, "logs"))
        write_json({"role": "user"}, name="0" * 64 + ".json", directory=os.path.join(build, "store", "blobs"))
        write_json({"kind": "nightly", "project": "p1", "build": "b1", "pr": ""}, name="job-1.json", directory=build)
        write_json({"kind": "presubmit", "project": "p1", "build": "b9", "pr": "4"}, name="job-2.json",
                   directory=build)
        write_json({"kind": "weekly"}, name="job-3.json", directory=build)
        with open(os.path.join(build, "job-4.json"), "w", encoding="utf-8") as handle:
            handle.write("{")
        quality = br.Quality()
        lines, manifests = br.load_lines([root], quality)
        r = br.build_report(lines, manifests, {}, prices(), quality)
        self.assertEqual(r["scopes"]["total"]["requests"], 2)
        self.assertEqual((r["data_quality"]["log_files"], r["data_quality"]["manifests"]), (2, 4))
        self.assertEqual(r["data_quality"]["bad_manifests"], 2)
        self.assertEqual(r["data_quality"]["manifest_builds_without_lines"], ["presubmit/b9"])

    def test_a_line_without_a_build_is_noted(self):
        r = report([line("a", 1, build="")])
        self.assertEqual(r["data_quality"]["lines_without_build"], 1)
        self.assertEqual(r["builds"][0]["build"], br.NO_BUILD)


class Window(unittest.TestCase):
    def test_whole_builds_by_their_first_request(self):
        r = report([line("a", 100, build="early"), line("b", 250, build="early"), line("c", 200, build="mid"),
                    line("d", 400, build="late")], since=150, until=300)
        self.assertEqual([b["build"] for b in r["builds"]], ["mid"])
        self.assertEqual(r["data_quality"]["skipped_lines"], {"outside_window": 3})

    def test_parse_time(self):
        self.assertEqual(br.parse_time("1762300800"), 1762300800.0)
        self.assertEqual(br.parse_time("2026-11-05"), 1793836800.0)
        self.assertEqual(br.parse_time("2026-11-05", end_of_day=True), 1793836800.0 + 86400)
        self.assertEqual(br.parse_time("2026-11-05T01:00:00"), 1793836800.0 + 3600)
        self.assertEqual(br.parse_time("2026-11-05T01:00:00+01:00"), 1793836800.0)
        with self.assertRaises(ValueError):
            br.parse_time("next tuesday")


class Definitions(unittest.TestCase):
    def test_the_readme_defines_every_metric(self):
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        for metric in br.METRICS + br.CASE_METRICS:
            self.assertTrue(f"`{metric.name}`" in readme, metric.name)

    def test_every_scope_carries_every_metric(self):
        r = report([line("a", 1)])
        for scope in br.SCOPES:
            self.assertEqual(set(br.METRIC_NAMES) - set(r["scopes"][scope]), set(), scope)


class Main(unittest.TestCase):
    def test_json_report_then_compare_from_saved_reports(self):
        logs = write([line("a", 1, group="model-default")])
        code, out, err = run_main([logs, "--price-as", "model-default=gemini-3.1-pro-preview", "--json"])
        self.assertEqual((code, err), (0, ""))
        saved = json.loads(out)
        self.assertEqual(saved["scopes"]["total"]["unpriced_requests"], 0)
        path = write_json(saved)
        code, out, _ = run_main(["--report", path, "--compare", path, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["scopes"]["total"]["requests"]["delta"], 0)
        code, out, _ = run_main([logs, "--compare", path])
        self.assertEqual(code, 0)
        self.assertIn("note: price tables or --price-as differ", out)

    def test_text_report(self):
        code, out, _ = run_main([write([line("a", 1)]), "--eval-data", write_json({"runs": EvalJoin.RUNS})])
        self.assertEqual(code, 0)
        self.assertIn("cache_hit_rate", out)
        self.assertIn("nightly cases", out)
        self.assertIn("unpriced by model {'m1': 1}", out)

    def test_bad_inputs_exit_2(self):
        logs = write([line("a", 1)])
        for argv in ([logs, "--price-as", "model-default=nope"], [logs, "--eval-data", write("x")],
                     [logs, "--since", "soon"], [logs, "--prices", write_json({"models": {}})]):
            code, _, err = run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.startswith("baseline_report: "), err)

    def test_argument_errors(self):
        with self.assertRaises(SystemExit):
            run_main([])
        with self.assertRaises(SystemExit):
            run_main(["--report", "x.json"])


if __name__ == "__main__":
    unittest.main()
