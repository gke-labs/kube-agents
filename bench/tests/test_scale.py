"""Tests for the harness-scale measurement runner's pure parts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kube_agents_bench.scale import collect, fixtures, kube, operations, records, runner
from kube_agents_bench.scale.collect import Snapshot
from kube_agents_bench.scale.config import load_plan

REPO_BENCH = Path(__file__).resolve().parents[1]
PHASE1 = REPO_BENCH / "scale" / "phase1.yaml"


def _record(run: int = 1, commit: str = "a" * 40, value: float = 1.0) -> records.Record:
    return records.Record(
        commit=commit, mode="next", phase="phase1", factor="clusters", level=8,
        operation="fleet_audit", run=run, started=f"2026-10-23T00:0{run}:00Z", ended="2026-10-23T00:30:00Z",
        metrics=[records.Metric("response_seconds", value), records.Metric("memory_peak_bytes", 5.0, "agent-api-auth")],
    )


def test_record_key_names_one_run():
    assert _record().key == "aaaaaaaa|next|clusters|8|fleet_audit|1"
    assert records.record_key("b" * 40, "today", "objects", 4000.0, "onboarding", 2) == "bbbbbbbb|today|objects|4000|onboarding|2"


def test_merge_replaces_same_key_and_keeps_others(tmp_path):
    path = tmp_path / "data.json"
    assert records.merge(path, [_record(1), _record(2)]) == 0
    assert records.merge(path, [_record(1, value=9.0), _record(1, commit="c" * 40)]) == 1
    data = json.loads(path.read_text())
    assert data["schema"] == records.SCHEMA_VERSION
    by_key = {r["key"]: r for r in data["records"]}
    assert len(by_key) == 3
    assert by_key["aaaaaaaa|next|clusters|8|fleet_audit|1"]["metrics"][0] == {"name": "response_seconds", "value": 9.0}
    assert by_key["aaaaaaaa|next|clusters|8|fleet_audit|1"]["metrics"][1]["container"] == "agent-api-auth"
    assert records.existing_keys(path) == set(by_key)


def test_load_refuses_another_schema(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"schema": 99, "records": []}))
    with pytest.raises(ValueError):
        records.load(path)


def test_phase1_matrix_runs_base_once_and_each_factor_only_for_its_operations():
    plan = load_plan(PHASE1, environ={}, validate_target=False)
    specs = plan.matrix()
    base = [s for s in specs if s.factor == "base"]
    assert len(base) == sum(op.runs for op in plan.operations)
    # A factor's base value is not re-run under the factor.
    assert not [s for s in specs if s.factor == "clusters" and s.level == plan.base["clusters"]]
    # Every non-base run uses an operation that lists its factor.
    assert all(s.factor in s.operation.factors for s in specs if s.factor != "base")
    assert plan.level_values("objects", 4000)["objects"] == 4000
    assert plan.level_values("objects", 4000)["clusters"] == plan.base["clusters"]


def test_environment_overrides_the_target():
    plan = load_plan(PHASE1, environ={"SCALE_PROJECT_ID": "other-project", "SCALE_MODE": "today"})
    assert plan.target["host_context"].startswith("gke_other-project_")
    assert plan.target["project_id"] == "other-project"
    assert plan.target["mode"] == "today"


def test_object_mix_matches_the_design_table():
    for level in (100, 4000, 10000):
        assert fixtures.object_mix(level).total == level
    assert fixtures.object_mix(4000).pods == 1200


def test_objects_manifests_hold_the_level_and_carry_the_label():
    manifests = fixtures.objects_manifests(100)
    deployments = [m for m in manifests if m["kind"] == "Deployment"]
    pods = sum(m["spec"]["replicas"] for m in deployments)
    assert len(manifests) + pods == 100
    assert all(m["metadata"]["labels"][fixtures.FIXTURE_LABEL] == "objects" for m in manifests)
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    requests = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
    assert requests["cpu"] == fixtures.POD_CPU_REQUEST


def test_fixture_pods_counts_every_fixture():
    values = {"clusters": 2, "objects": 4000, "failing_pods": 100, "listing_pods": 100, "churn_per_minute": 1000}
    # Each of 2 clusters takes half the churn; only the first holds the listing and the failing workload.
    assert fixtures.fixture_pods(values, 30) == 1200 + 250 + 100 + 100
    assert fixtures.fixture_pods(values, 30, first=False) == 1200 + 250


PROMETHEUS_TEXT = """# HELP k8s_event_watcher_events_seen_total Events seen.
# TYPE k8s_event_watcher_events_seen_total counter
k8s_event_watcher_events_seen_total{cluster="a"} 10
k8s_event_watcher_events_seen_total{cluster="b"} 5
kubeagents_credential_proxy_admission_wait_seconds_bucket{le="1"} 4
kubeagents_credential_proxy_admission_wait_seconds_bucket{le="15"} 9
kubeagents_credential_proxy_admission_wait_seconds_bucket{le="+Inf"} 10
litellm_input_tokens_metric_total{model="m"} 1000
"""


def test_parse_prometheus_sums_families_and_reads_buckets():
    samples = collect.parse_prometheus(PROMETHEUS_TEXT)
    assert collect.family_sum(samples, "k8s_event_watcher_events_seen_total") == 15
    buckets = collect.histogram_buckets(samples, "kubeagents_credential_proxy_admission_wait_seconds")
    assert buckets == {1.0: 4, 15.0: 9, float("inf"): 10}
    assert collect.histogram_quantile(buckets, 0.5) == 15.0
    assert collect.histogram_quantile({}, 0.5) is None


def test_counter_metrics_sum_tokens_over_replicas():
    before = Snapshot(scraped={"litellm": {"a": collect.parse_prometheus(PROMETHEUS_TEXT),
                                           "b": collect.parse_prometheus(PROMETHEUS_TEXT)}})
    after_text = PROMETHEUS_TEXT.replace("1000", "1600")
    after = Snapshot(scraped={"litellm": {"a": collect.parse_prometheus(after_text),
                                          "b": collect.parse_prometheus(after_text)}})
    metrics = {(m.name, m.container): m.value for m in collect.counter_metrics(before, after)}
    assert metrics[("tokens_input", None)] == 1200


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2Gi", 2 * 1024**3), ("128Mi", 128 * 1024**2), ("500m", 0.5), ("1", 1.0), ("1G", 1e9)],
)
def test_parse_quantity(text, expected):
    assert collect.parse_quantity(text) == pytest.approx(expected)


ENV = {"SCALE_PROJECT_ID": "example-project"}


def test_target_names_no_project_and_is_validated():
    with pytest.raises(ValueError, match="SCALE_PROJECT_ID"):
        load_plan(PHASE1, environ={})
    plan = load_plan(PHASE1, environ=ENV)
    assert plan.target["host_context"] == "gke_example-project_us-central1-c_scale-host"
    with pytest.raises(ValueError, match="is not the host cluster's context"):
        load_plan(PHASE1, environ={**ENV, "SCALE_HOST_CONTEXT": "gke_example-project_us-central1_platform-agent-host"})
    # plan needs no target at all
    assert load_plan(PHASE1, environ={}, validate_target=False).matrix()


def _scraped(pod: str, text: str) -> dict[str, dict[str, list]]:
    return {"watcher": {pod: collect.parse_prometheus(text)}}


def test_counter_growth_counts_a_replaced_pod_from_zero():
    old_text = "k8s_event_watcher_events_seen_total 20000\nk8s_event_watcher_session_creates_total 900\n"
    new_text = "k8s_event_watcher_events_seen_total 500\nk8s_event_watcher_session_creates_total 40\n"
    before = Snapshot(restarts={("gw-old", "agent-api-auth"): 3}, scraped=_scraped("gw-old", old_text))
    after = Snapshot(
        restarts={("gw-new", "agent-api-auth"): 1}, oom={("gw-new", "agent-api-auth")},
        scraped=_scraped("gw-new", new_text),
    )
    metrics = {(m.name, m.container): m.value for m in collect.counter_metrics(before, after)}
    assert metrics[("restarts", "agent-api-auth")] == 1
    assert metrics[("oomkills", "agent-api-auth")] == 1
    assert metrics[("watcher_events_seen", None)] == 500
    assert metrics[("watcher_session_creates", None)] == 40


def test_counter_growth_on_a_kept_pod_is_the_difference():
    before = Snapshot(restarts={("gw", "c"): 2}, scraped=_scraped("gw", "k8s_event_watcher_events_seen_total 10\n"))
    after = Snapshot(restarts={("gw", "c"): 2}, scraped=_scraped("gw", "k8s_event_watcher_events_seen_total 25\n"))
    metrics = {(m.name, m.container): m.value for m in collect.counter_metrics(before, after)}
    assert metrics[("restarts", "c")] == 0
    assert metrics[("oomkills", "c")] == 0
    assert metrics[("watcher_events_seen", None)] == 15


def test_churner_carries_fractions_so_low_rates_hold():
    churner = fixtures.Churner(["a", "b"], per_minute=10, lifetime_seconds=30)
    batches_per_minute = 60 // fixtures.CHURN_BATCH_SECONDS
    per_cluster = sum(churner.next_batch() for _ in range(batches_per_minute * 10))
    # 10 per minute across 2 clusters is 5 per cluster per minute, over 10 minutes.
    assert per_cluster == 50


def test_a_failed_prompt_is_counted_not_timed():
    outcome = operations.OperationResult()
    operations.fold_answer(outcome, {"seconds": 2.0, "output_chars": 0, "tokens": {},
                                     "errors": ["KUBE_AGENTS_INFRA_FAILURE: transport"]})
    operations.fold_answer(outcome, {"seconds": 40.0, "output_chars": 120,
                                     "tokens": {"input": 10, "output": 3, "cached": 7}, "errors": []})
    assert outcome.response_seconds == [40.0]
    assert outcome.failed_prompts == 1
    assert outcome.errors[0].startswith(operations.PROMPT_FAILED)
    assert outcome.tokens == {"tokens_input": 10, "tokens_output": 3, "tokens_cached": 7}


def test_commit_of_reads_a_commit_pinned_tag_only():
    sha = "eeb8a95ceb0a9efd66434161f87820b35b65360c"
    assert runner.commit_of(f"ghcr.io/gke-labs/kube-agents/platform-agent:{sha}") == sha
    assert runner.commit_of("ghcr.io/gke-labs/kube-agents/platform-agent:0.9.0") == ""
    assert runner.commit_of("registry.example:5000/platform-agent") == ""


class _FailingInstall:
    def container_peaks(self, start, end):
        raise kube.CommandError("gcloud auth application-default print-access-token exited 1")


def test_a_monitoring_failure_costs_the_record_its_peaks_only():
    plan = load_plan(PHASE1, environ=ENV)
    run = runner.Runner(plan, Path("/nonexistent/harness-limits.json"))
    run.install = _FailingInstall()
    record = _record()
    run.add_peaks(record)
    assert record.errors and record.errors[0].startswith("monitoring: CommandError")


def test_run_retries_transient_failures_only(monkeypatch):
    calls = []

    class Proc:
        def __init__(self, code, err):
            self.returncode, self.stderr, self.stdout = code, err, "ok"

    outcomes = [Proc(1, "print credential failed: failed to retrieve access token"), Proc(0, "")]
    monkeypatch.setattr(kube.subprocess, "run", lambda *a, **k: calls.append(1) or outcomes.pop(0))
    monkeypatch.setattr(kube.time, "sleep", lambda s: None)
    assert kube.run(["kubectl", "get", "pods"]) == "ok"
    assert len(calls) == 2
    monkeypatch.setattr(kube.subprocess, "run", lambda *a, **k: Proc(1, "Error from server (NotFound)"))
    with pytest.raises(kube.CommandError):
        kube.run(["kubectl", "get", "pods"])


def test_a_host_named_like_the_prefix_is_not_a_test_cluster():
    plan = load_plan(PHASE1, environ=ENV)
    run = runner.Runner(plan, Path("/nonexistent/harness-limits.json"))
    assert run.test_clusters({"scale-host", "scale-01", "scale-02", "other-01"}) == {"scale-01", "scale-02"}


def test_record_keys_keep_a_non_sha_build_whole():
    assert records.record_key("rc_2610220514_eeb8a95", "next", "base", 0, "fan_out", 1).startswith("rc_2610220514_eeb8a95|")
    assert records.record_key("rc_2610221000_abc1234", "next", "base", 0, "fan_out", 1) != records.record_key(
        "rc_2610220514_eeb8a95", "next", "base", 0, "fan_out", 1)


def test_a_rate_limited_failure_is_a_429_not_a_failed_prompt():
    outcome = operations.OperationResult()
    operations.fold_answer(outcome, {"seconds": 1.0, "output_chars": 0, "tokens": {},
                                     "errors": ["KUBE_AGENTS_INFRA_FAILURE: rate_limit: RESOURCE_EXHAUSTED"]})
    assert outcome.failed_prompts == 0
    assert outcome.provider_429s == 1
    assert outcome.response_seconds == []


def test_agent_env_clears_the_inject_door(monkeypatch):
    monkeypatch.setattr(operations.kube, "kubectl", lambda *a, **k: "dG9rZW4=")
    env = operations.agent_env("ctx", "ns", 12345)
    assert env["AGENT_TRANSPORT"] == "api" and env["AGENT_INJECT_TOKEN"] == "" and env["AGENT_LOCAL_PORT"] == "12345"


def test_triage_lost_counts_every_marker():
    pattern = collect.LOG_PATTERNS["triage_lost"]
    text = ("Aborting troubleshooting trigger\nGateway API chat execution failed (status 502)\n"
            "TRIAGE_NOT_STARTED session=s reason=timeout\nunrelated line\n")
    assert len(pattern.findall(text)) == 3


class _ProfileInstall(collect.Install):
    def __init__(self, listing):
        super().__init__("example-project", "ctx", "ns", "host", "us-central1-c")
        self.listing = listing

    def gateway_pod(self):
        return {"metadata": {"name": "gw"}}


def test_profile_clusters_parses_the_scaffolder_names(monkeypatch):
    install = _ProfileInstall("")
    listing = ("cluster-example-project-scale-01-us-central1-c\ncluster-example-project-a-b-c-us-east4\n"
               "platform\ncluster-other-project-x-us-central1-a\n")
    monkeypatch.setattr(collect.kube, "kubectl", lambda *a, **k: listing)
    assert install.profile_clusters() == {"scale-01", "a-b-c"}


def test_pending_keys_follow_the_install_build(monkeypatch, tmp_path):
    plan = load_plan(PHASE1, environ=ENV)
    path = tmp_path / "data.json"
    records.merge(path, [records.Record(
        commit="eeb8a95ceb0a9efd66434161f87820b35b65360c", mode="next", phase="phase1", factor="base",
        level=0, operation="fan_out", run=1, started="2026-10-23T00:00:00Z", ended="2026-10-23T00:10:00Z")])
    run = runner.Runner(plan, path)

    class Install:
        def image(self):
            return "ghcr.io/x/platform-agent:eeb8a95ceb0a9efd66434161f87820b35b65360c"

        def mode(self):
            return "next"

    run.install = Install()
    run.install.context, run.install.namespace = "ctx", "ns"
    monkeypatch.setattr(runner.operations, "wait_gateway_ready", lambda *a, **k: None)
    specs = [s for s in plan.matrix() if s.factor == "base" and s.operation.name == "fan_out"]
    assert [s.run for s in run.pending(specs)] == [2, 3]
    run.install.image = lambda: "ghcr.io/x/platform-agent:fffffffffffffffffffffffffffffffffffffff0"
    assert [s.run for s in run.pending(specs)] == [1, 2, 3]


def test_record_round_trips_through_its_dict():
    record = _record()
    again = records.Record.from_dict(record.to_dict())
    assert again.key == record.key and again.metrics[1].container == "agent-api-auth"


def test_reset_is_refused_without_the_host_label(monkeypatch):
    plan = load_plan(PHASE1, environ=ENV)
    run = runner.Runner(plan, Path("/nonexistent/harness-limits.json"))
    monkeypatch.setattr(runner.kube, "cluster_labels", lambda *a: {})
    with pytest.raises(kube.CommandError, match="refusing to reset"):
        run.check_resettable()
    monkeypatch.setattr(runner.kube, "cluster_labels", lambda *a: {"purpose": "harness-scale"})
    run.check_resettable()


def test_a_harness_error_before_any_model_call_is_a_failed_prompt():
    outcome = operations.OperationResult()
    operations.fold_answer(outcome, {"seconds": 0.7, "output_chars": 40, "tokens": {},
                                     "errors": ["AGENT_TRANSPORT must be one of ['api', 'inject'], got ''"]})
    assert outcome.failed_prompts == 1 and outcome.response_seconds == []


def test_churn_batch_ids_never_read_as_numbers():
    churner = fixtures.Churner(["a"], per_minute=60, lifetime_seconds=30)
    for _ in range(200):
        batch = churner._batch(1)[0]["metadata"]["labels"][fixtures.BATCH_LABEL]
        assert batch[0].isalpha()


def test_an_answer_without_a_model_call_is_a_failed_prompt():
    outcome = operations.OperationResult()
    operations.fold_answer(outcome, {"seconds": 9.0, "output_chars": 300, "tokens": {"input": 0, "output": 0},
                                     "errors": [], "preview": "I ran this audit a minute ago"})
    assert outcome.failed_prompts == 1 and outcome.response_seconds == []
    assert "no model call" in outcome.errors[0] and outcome.previews == ["I ran this audit a minute ago"]


def test_counters_follow_a_pod_recreated_under_its_old_name():
    # A StatefulSet re-creates platform-agent-shell-0 with a new UID; the new pod counts from zero.
    before = Snapshot(restarts={("uid-old", "shell"): 3})
    after = Snapshot(restarts={("uid-new", "shell"): 2}, oom={("uid-new", "shell")})
    metrics = {(m.name, m.container): m.value for m in collect.counter_metrics(before, after)}
    assert metrics[("restarts", "shell")] == 2 and metrics[("oomkills", "shell")] == 1


def test_unfinished_answers_are_recognized():
    assert operations.unfinished_reason("NO_REGISTERED_REPO: this install has no registered GitOps repository", [])
    assert operations.unfinished_reason("The fleet audit was cancelled because ... explicitly excluded from the audit scope", [])
    assert operations.unfinished_reason("checking every cluster.", ["KUBE_AGENTS_DELEGATION_CEILING: delegated tasks did not finish"])
    assert operations.unfinished_reason("Of the 8 clusters checked, only one is stressed.", []) == ""


def test_reclassify_excludes_unfinished_and_unchecked_runs_and_scrubs_the_project(tmp_path):
    plan = load_plan(PHASE1, environ=ENV)
    path = tmp_path / "data.json"
    base = dict(commit="e" * 40, mode="next", phase="phase1", level=0, started="2026-10-24T00:00:00Z",
                ended="2026-10-24T00:05:00Z")
    records.merge(path, [
        records.Record(factor="base", operation="fleet_audit", run=1, **base),                      # no answers
        records.Record(factor="base", operation="fan_out", run=1, **base),                          # no answers, fine
        records.Record(factor="clusters", operation="fleet_audit", run=2, answers=["NO_REGISTERED_REPO here"],
                       errors=["kubectl --context gke_example-project_x"], **{**base, "level": 8}),
    ])
    run = runner.Runner(plan, path)
    assert run.reclassify() == 2
    by = {r["key"].split("|", 2)[2]: r for r in records.load(path)["records"]}
    assert by["base|0|fleet_audit|1"]["excluded_from"] == ["response_seconds"]
    assert by["base|0|fan_out|1"]["excluded_from"] == []
    assert by["clusters|8|fleet_audit|2"]["excluded_from"] == ["response_seconds"]
    assert "example-project" not in json.dumps(by["clusters|8|fleet_audit|2"])
