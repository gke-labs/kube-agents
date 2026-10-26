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


def test_a_counter_reset_in_place_counts_from_zero():
    # An OOMKill restarts agent-api-auth in place: same pod UID, counters back to zero.
    before = Snapshot(scraped={"watcher": {"uid": collect.parse_prometheus("k8s_event_watcher_events_seen_total 900\n")},
                               "proxy": {"uid": collect.parse_prometheus(
                                   'kubeagents_credential_proxy_admission_refusals_total 7\n'
                                   'kubeagents_credential_proxy_admission_wait_seconds_bucket{le="1"} 50\n'
                                   'kubeagents_credential_proxy_admission_wait_seconds_bucket{le="+Inf"} 60\n')}})
    after = Snapshot(scraped={"watcher": {"uid": collect.parse_prometheus("k8s_event_watcher_events_seen_total 40\n")},
                              "proxy": {"uid": collect.parse_prometheus(
                                  'kubeagents_credential_proxy_admission_refusals_total 2\n'
                                  'kubeagents_credential_proxy_admission_wait_seconds_bucket{le="1"} 3\n'
                                  'kubeagents_credential_proxy_admission_wait_seconds_bucket{le="+Inf"} 4\n')}})
    metrics = {m.name: m.value for m in collect.counter_metrics(before, after) if m.container is None}
    assert metrics["watcher_events_seen"] == 40
    assert metrics["proxy_refusals"] == 2
    assert metrics["proxy_wait_p50_seconds"] == 1.0


def test_a_timeout_is_raised_at_once(monkeypatch):
    calls = []

    def hang(*args, **kwargs):
        calls.append(1)
        raise kube.subprocess.TimeoutExpired(args[0], 1)

    monkeypatch.setattr(kube.subprocess, "run", hang)
    monkeypatch.setattr(kube.time, "sleep", lambda s: None)
    with pytest.raises(kube.CommandError, match="timed out"):
        kube.run(["gcloud", "container", "clusters", "create", "x"])
    assert len(calls) == 1


class _EmptyInstall:
    cluster, namespace = "host", "ns"

    def container_peaks(self, start, end):
        return {}


def test_an_empty_monitoring_answer_is_an_error_and_not_retried(tmp_path):
    plan = load_plan(PHASE1, environ=ENV)
    path = tmp_path / "data.json"
    run = runner.Runner(plan, path)
    run.install = _EmptyInstall()
    record = _record()
    run.add_peaks(record)
    assert record.errors and record.errors[0].startswith("monitoring: no container series")
    records.merge(path, [record])
    run.install = _FailingInstall()  # would raise if queried again
    run.backfill_peaks()
    assert records.load(path)["records"][0]["errors"] == record.errors


def test_reclassify_counts_unfinished_prompts_and_trusts_failed_ones(tmp_path):
    plan = load_plan(PHASE1, environ=ENV)
    path = tmp_path / "data.json"
    base = dict(commit="e" * 40, mode="next", phase="phase1", factor="clusters", level=8,
                started="2026-10-24T00:00:00Z", ended="2026-10-24T00:05:00Z", operation="fleet_audit")
    records.merge(path, [
        records.Record(run=1, answers=["Audit cancelled as all 8 managed clusters are explicitly excluded"], **base),
        records.Record(run=2, answers=["Checked 8 clusters; 3 findings"], **base),
        records.Record(run=3, errors=["prompt failed: no answer within 3600s"], **base),
    ])
    runner.Runner(plan, path).reclassify()
    by = {r["run"]: r for r in records.load(path)["records"]}
    metric = {run: next(m["value"] for m in by[run]["metrics"] if m["name"] == "unfinished_prompts") for run in by}
    assert metric == {1: 1.0, 2: 0.0, 3: 0.0}
    assert not any("written before answers were kept" in e for e in by[3]["errors"])


def test_a_relay_that_dies_at_start_removes_its_kubeconfig(monkeypatch):
    monkeypatch.setattr(operations.kube, "kubectl", lambda *a, **k: "apiVersion: v1\n")
    monkeypatch.setattr(operations.time, "sleep", lambda s: None)

    class Dead:
        def poll(self):
            return 1

    monkeypatch.setattr(operations.subprocess, "Popen", lambda *a, **k: Dead())
    relay = operations.Relay("ctx", "ns")
    with pytest.raises(kube.CommandError):
        relay.start()
    assert not relay.kubeconfig.exists()


def test_the_nats_wait_survives_the_moment_the_pod_does_not_exist(monkeypatch):
    replies = [kube.CommandError('pods "platform-agent-a2a-nats-0" not found'),
               kube.CommandError('pods "platform-agent-a2a-nats-0" not found'), "condition met"]

    def kubectl(*args, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(operations.kube, "kubectl", kubectl)
    monkeypatch.setattr(operations.time, "sleep", lambda s: None)
    operations._wait_pod_ready("ctx", "ns", operations.NATS_POD)
    assert replies == []


def test_reclassify_counts_a_delegation_wait_once_per_marker(tmp_path):
    plan = load_plan(PHASE1, environ=ENV)
    path = tmp_path / "data.json"
    records.merge(path, [records.Record(
        commit="e" * 40, mode="next", phase="phase1", factor="concurrent", level=15, operation="fan_out", run=1,
        started="2026-10-24T00:00:00Z", ended="2026-10-24T00:40:00Z",
        answers=["checking all 2 clusters.", "I monitor scale-01 and scale-02.", ""],
        errors=["KUBE_AGENTS_DELEGATION_CEILING: delegated tasks did not finish within 1800s"])])
    runner.Runner(plan, path).reclassify()
    record = records.load(path)["records"][0]
    assert next(m["value"] for m in record["metrics"] if m["name"] == "unfinished_prompts") == 1.0


def test_a_plan_without_mode_is_refused_at_load(tmp_path):
    text = PHASE1.read_text().replace("  mode: next\n", "")
    trimmed = tmp_path / "plan.yaml"
    trimmed.write_text(text)
    with pytest.raises(ValueError, match="SCALE_MODE"):
        load_plan(trimmed, environ=ENV)


PHASE2 = REPO_BENCH / "scale" / "phase2.yaml"


def test_phase2_runs_levels_in_plan_order_and_reuses_the_phase1_base():
    plan = load_plan(PHASE2, environ=ENV)
    levels = list(dict.fromkeys((s.factor, s.level) for s in plan.matrix()))
    assert levels[0] == ("base", 0)
    assert [f for f, _ in levels[1:]] == ["proxy_concurrency"] * 4 + ["concurrent"] * 3 + ["failing_pods"] * 2 + [
        "combined", "clusters", "clusters"]
    assert plan.level_values("combined", 1) == {**plan.base, "clusters": 8, "objects": 4000}
    phase1 = load_plan(PHASE1, environ=ENV)
    shared = {s.operation.name for s in plan.matrix() if s.factor == "base"} & {o.name for o in phase1.operations}
    assert shared == {"onboarding", "watcher_soak", "fan_out", "boot_burst"}
    # Same base fleet, so the base keys the Phase 1 runs wrote stand for Phase 2's base too.
    assert plan.level_values("base", 0).items() >= phase1.level_values("base", 0).items()


def test_a_combined_level_must_name_a_point(tmp_path):
    text = PHASE2.read_text().replace("combined: [1]", "combined: [1, 2]")
    path = tmp_path / "plan.yaml"
    path.write_text(text)
    with pytest.raises(ValueError, match="combined levels"):
        load_plan(path, environ=ENV)


def test_proxy_load_reads_the_workers_summary(monkeypatch):
    calls = []
    summary = {"completed": 90, "errors": 2, "wall": 180.0, "p50": 4.5, "p95": 9.0, "max": 12.0,
               "first_error": "error: You must be logged in"}
    monkeypatch.setattr(operations.kube, "kubectl",
                        lambda *a, **k: calls.append((a, k)) or "noise\n" + json.dumps(summary) + "\n")
    op = load_plan(PHASE2, environ=ENV).operations[-1]
    outcome = operations.proxy_load("ctx", "ns", op, 8, "/opt/data/profiles/cluster-p-scale-01-*/kubeconfig.yaml")
    assert outcome.metrics == {"proxy_load_completed": 90.0, "proxy_load_errors": 2.0, "proxy_load_per_second": 0.5,
                               "proxy_load_p50_seconds": 4.5, "proxy_load_p95_seconds": 9.0,
                               "proxy_load_max_seconds": 12.0}
    assert outcome.errors == ["proxy load command error: error: You must be logged in"]
    args, kwargs = calls[0]
    assert args[-4:-1] == ("8", "180", json.dumps(["credential-proxy-exec", *op.command]))
    assert kwargs["stdin"] == operations.PROXY_LOAD_SCRIPT


def test_a_proxy_load_that_cannot_run_is_an_error(monkeypatch):
    def fail(*a, **k):
        raise kube.CommandError("kubectl exec... exited 1: IndexError: list index out of range")
    monkeypatch.setattr(operations.kube, "kubectl", fail)
    op = load_plan(PHASE2, environ=ENV).operations[-1]
    outcome = operations.proxy_load("ctx", "ns", op, 1, "/nowhere/*")
    assert outcome.metrics == {} and outcome.errors[0].startswith("proxy load failed")


def test_counter_metrics_count_brokered_commands_and_their_seconds():
    before = Snapshot(scraped={"proxy": {"uid": collect.parse_prometheus(
        'kubeagents_tool_invocations_total{tool="kubectl",subcommand="get",status="success"} 20\n'
        'kubeagents_tool_execution_duration_seconds_sum{tool="kubectl"} 25.0\n')}})
    after = Snapshot(scraped={"proxy": {"uid": collect.parse_prometheus(
        'kubeagents_tool_invocations_total{tool="kubectl",subcommand="get",status="success"} 50\n'
        'kubeagents_tool_invocations_total{tool="gcloud",subcommand="container",status="success"} 4\n'
        'kubeagents_tool_execution_duration_seconds_sum{tool="kubectl"} 61.0\n'
        'kubeagents_tool_execution_duration_seconds_sum{tool="gcloud"} 8.0\n')}})
    metrics = {m.name: m.value for m in collect.counter_metrics(before, after)}
    assert metrics["proxy_commands"] == 34
    assert metrics["proxy_command_seconds"] == 44.0


def _registration_runner(monkeypatch, tmp_path, profiles: list[set[str]], watched: set[str]):
    plan = load_plan(PHASE2, environ=ENV)
    run = runner.Runner(plan, tmp_path / "data.json")
    reads = iter(profiles)
    last = {}

    class Install:
        context, namespace = "ctx", "ns"

        def profile_clusters(self):
            last["value"] = next(reads, last.get("value"))
            return last["value"]

        def watched_clusters(self):
            return watched

    run.install = Install()
    reconciles = []
    monkeypatch.setattr(runner.operations, "reconcile_profiles", lambda *a: reconciles.append(a))
    monkeypatch.setattr(runner.operations, "restart", lambda *a: None)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    return run, reconciles


def test_registration_runs_the_reconcile_when_the_profiles_differ(monkeypatch, tmp_path):
    fleet = {"scale-01", "scale-02", "scale-03"}
    run, reconciles = _registration_runner(monkeypatch, tmp_path, [{"scale-01", "scale-02", "scale-09"}, fleet], fleet)
    run.wait_registered(3)
    assert reconciles == [("ctx", "ns")]
    assert run.watch_errors == []


def test_registration_skips_the_reconcile_when_the_profiles_match(monkeypatch, tmp_path):
    fleet = {"scale-01", "scale-02"}
    run, reconciles = _registration_runner(monkeypatch, tmp_path, [fleet | {"host"}], fleet)
    run.wait_registered(2)
    assert reconciles == [] and run.watch_errors == []


def test_a_level_that_keeps_the_fleet_skips_the_settle(monkeypatch, tmp_path):
    run = runner.Runner(load_plan(PHASE2, environ=ENV), tmp_path / "data.json")
    sleeps = []
    for name in ("preflight", "ensure_clusters", "ensure_nodes", "ensure_fixtures"):
        monkeypatch.setattr(run, name, lambda *a: None)
    monkeypatch.setattr(runner.fixtures.Churner, "start", lambda self: None)
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    run.apply_level("proxy_concurrency", 4)
    run.apply_level("proxy_concurrency", 8)
    run.apply_level("concurrent", 8)
    run.apply_level("failing_pods", 10)
    assert sleeps == [300, 300]
