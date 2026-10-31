"""Tests for the harness-scale measurement runner's pure parts."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import types
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
    base = dict(commit="e" * 40, mode="next", phase="phase1", level=0, started="2026-10-24T00:10:00Z",
                ended="2026-10-24T00:15:00Z")
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
    assert by["clusters|8|fleet_audit|2"]["excluded_from"] == ["response_seconds", *runner.RESOURCE_FITS, *runner.TOKEN_FITS]
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
                started="2026-10-24T00:10:00Z", ended="2026-10-24T00:15:00Z", operation="fleet_audit")
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
    # A prompt answered without its operation measures none of its cost; a completed one stays in.
    assert {"memory_peak_bytes", "cpu_peak_cores", "tokens_input"} <= set(by[1]["excluded_from"])
    assert by[2]["excluded_from"] == []


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
        "combined", "clusters"]
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
    # A command refused by policy or by the slot wait is an invocation that never ran: not counted.
    before = Snapshot(scraped={"proxy": {"uid": collect.parse_prometheus(
        'kubeagents_tool_invocations_total{tool="kubectl",subcommand="get",status="success"} 20\n'
        'kubeagents_tool_execution_duration_seconds_count{tool="kubectl"} 20\n'
        'kubeagents_tool_execution_duration_seconds_sum{tool="kubectl"} 25.0\n')}})
    after = Snapshot(scraped={"proxy": {"uid": collect.parse_prometheus(
        'kubeagents_tool_invocations_total{tool="kubectl",subcommand="get",status="success"} 50\n'
        'kubeagents_tool_invocations_total{tool="kubectl",subcommand="delete",status="blocked"} 6\n'
        'kubeagents_tool_invocations_total{tool="gcloud",subcommand="container",status="success"} 4\n'
        'kubeagents_tool_execution_duration_seconds_count{tool="kubectl"} 50\n'
        'kubeagents_tool_execution_duration_seconds_count{tool="gcloud"} 4\n'
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
    # The runner's clock only: a library polling a subprocess in another thread sleeps too.
    monkeypatch.setattr(runner, "time", types.SimpleNamespace(sleep=sleeps.append, monotonic=time.monotonic,
                                                              time=time.time))
    run.apply_level("proxy_concurrency", 4)
    run.apply_level("proxy_concurrency", 8)
    run.apply_level("concurrent", 8)
    run.apply_level("failing_pods", 10)
    assert sleeps == [300, 300]


def test_requests_and_limits_are_read_per_container(monkeypatch):
    install = collect.Install("p", "ctx", "ns", "host", "zone")
    pod = {"spec": {"containers": [{"name": "envoy-credential-proxy", "resources": {
        "requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}}}],
        "initContainers": [{"name": "fluent-bit", "restartPolicy": "Always",
                            "resources": {"requests": {"memory": "64Mi"}}}]}}
    monkeypatch.setattr(install, "pods", lambda *a: [pod])
    assert install.requests() == {"envoy-credential-proxy": {"memory_bytes": 1024**3, "cpu_cores": 0.5},
                                  "fluent-bit": {"memory_bytes": 64 * 1024**2}}
    assert install.limits() == {"envoy-credential-proxy": {"memory_bytes": 2 * 1024**3, "cpu_cores": 1.0}}


def test_a_restarted_watcher_is_polled_until_it_reports_the_fleet(monkeypatch, tmp_path):
    fleet = {"scale-01", "scale-02"}
    run = runner.Runner(load_plan(PHASE2, environ=ENV), tmp_path / "data.json")
    reports = iter([{"scale-01"}, {"scale-01"}, fleet])
    run.install = type("Install", (), {"watched_clusters": lambda self: next(reports)})()
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    assert run._wait_watched(fleet) == fleet


def test_cluster_creation_runs_at_most_the_bound_at_once(monkeypatch, tmp_path):
    run = runner.Runner(load_plan(PHASE2, environ=ENV), tmp_path / "data.json")
    run.settings["cluster_create_parallelism"] = 2
    lock, live, peak = threading.Lock(), [0], [0]

    def create(name):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1

    monkeypatch.setattr(runner.kube, "clusters_with_prefix", lambda *a: [])
    monkeypatch.setattr(run, "_create_cluster", create)
    monkeypatch.setattr(runner.kube, "get_credentials", lambda *a: None)
    monkeypatch.setattr(run, "wait_registered", lambda count: None)
    run.ensure_clusters(5)
    assert peak[0] == 2


def test_a_failed_reconcile_falls_back_to_waiting(monkeypatch, tmp_path):
    fleet = {"scale-01", "scale-02", "scale-03"}
    run, _ = _registration_runner(monkeypatch, tmp_path, [{"scale-01"}, {"scale-01"}, fleet], fleet)

    def fail(*a):
        raise kube.CommandError("exec failed")
    monkeypatch.setattr(runner.operations, "reconcile_profiles", fail)
    run.wait_registered(3)
    assert run.watch_errors == []


def test_a_new_objects_level_replaces_the_old_one(monkeypatch, tmp_path):
    run = runner.Runner(load_plan(PHASE2, environ=ENV), tmp_path / "data.json")
    steps = []
    monkeypatch.setattr(run, "_applied_level", lambda context, fixture: 4000)
    monkeypatch.setattr(runner.fixtures, "delete_fixture", lambda context, fixture: steps.append("delete"))
    monkeypatch.setattr(run, "_wait_fixture_gone", lambda context, fixture: steps.append("wait"))
    monkeypatch.setattr(runner.kube, "apply", lambda context, manifests: steps.append("apply"))
    run._ensure_objects("ctx", 100)
    assert steps == ["delete", "wait", "apply"]
    steps.clear()
    monkeypatch.setattr(run, "_applied_level", lambda context, fixture: 100)
    run._ensure_objects("ctx", 100)
    assert steps == ["apply"]


def test_a_stored_combined_run_of_another_point_is_pending(monkeypatch, tmp_path):
    plan = load_plan(PHASE2, environ=ENV)
    path = tmp_path / "data.json"
    commit = "eeb8a95ceb0a9efd66434161f87820b35b65360c"
    records.merge(path, [records.Record(
        commit=commit, mode="next", phase="phase2", factor="combined", level=1, operation="fan_out", run=run_number,
        started="2026-10-27T00:00:00Z", ended="2026-10-27T00:10:00Z",
        values={**plan.base, "clusters": 8.0, "objects": objects}) for run_number, objects in ((1, 4000.0), (2, 999.0))])
    run = runner.Runner(plan, path)
    run.install = type("Install", (), {"image": lambda self: f"x:{commit}", "mode": lambda self: "next",
                                       "context": "ctx", "namespace": "ns"})()
    monkeypatch.setattr(runner.operations, "wait_gateway_ready", lambda *a, **k: None)
    specs = [s for s in plan.matrix() if s.factor == "combined" and s.operation.name == "fan_out"]
    assert [s.run for s in run.pending(specs)] == [2, 3]


def test_a_proxy_load_needs_a_list_command(tmp_path):
    text = PHASE2.read_text().replace(
        "    command:\n      - kubectl\n      - --kubeconfig\n      - \"{kubeconfig}\"\n      - get\n      - pods\n"
        "      - --all-namespaces\n      - --no-headers\n", "    command: kubectl get pods\n")
    path = tmp_path / "plan.yaml"
    path.write_text(text)
    with pytest.raises(ValueError, match="needs a command"):
        load_plan(path, environ=ENV)


def test_a_level_that_failed_half_way_is_settled_next_time(monkeypatch, tmp_path):
    run = runner.Runner(load_plan(PHASE2, environ=ENV), tmp_path / "data.json")
    sleeps = []
    for name in ("preflight", "ensure_clusters", "ensure_nodes"):
        monkeypatch.setattr(run, name, lambda *a: None)
    monkeypatch.setattr(runner.fixtures.Churner, "start", lambda self: None)
    monkeypatch.setattr(runner, "time", types.SimpleNamespace(sleep=sleeps.append, monotonic=time.monotonic,
                                                              time=time.time))
    monkeypatch.setattr(run, "ensure_fixtures", lambda *a: None)
    run.apply_level("proxy_concurrency", 4)

    def broken(*a):
        raise kube.CommandError("apply failed")
    monkeypatch.setattr(run, "ensure_fixtures", broken)
    with pytest.raises(kube.CommandError):
        run.apply_level("failing_pods", 10)
    monkeypatch.setattr(run, "ensure_fixtures", lambda *a: None)
    run.apply_level("proxy_concurrency", 8)
    assert sleeps == [300, 300]


def test_the_default_pool_holds_fixture_pods_up_to_its_share(monkeypatch, tmp_path):
    run = runner.Runner(load_plan(PHASE2, environ=ENV), tmp_path / "data.json")
    sized = {}
    monkeypatch.setattr(runner.kube, "ensure_node_pool",
                        lambda project, zone, cluster, pool, machine, size: sized.__setitem__(cluster, size))
    values = run.plan.level_values("clusters", 16)
    run.settings["default_pool_pods"] = 0
    run.ensure_nodes(values)
    assert set(sized.values()) == {1, 2}
    run.settings["default_pool_pods"] = 150
    run.ensure_nodes(values)
    assert set(sized.values()) == {0}


def test_an_empty_fixture_pool_is_not_created(monkeypatch):
    calls = []
    monkeypatch.setattr(kube, "node_pool_size", lambda *a: None)
    monkeypatch.setattr(kube, "gcloud", lambda *a, **k: calls.append(a))
    kube.ensure_node_pool("p", "z", "c", "pool", "n2-standard-2", 0)
    assert calls == []


def test_a_pool_that_cannot_be_emptied_does_not_stop_the_level(monkeypatch, tmp_path):
    run = runner.Runner(load_plan(PHASE2, environ=ENV), tmp_path / "data.json")
    run.settings["default_pool_pods"] = 150

    def resize(project, zone, cluster, pool, machine, size):
        raise kube.CommandError("Cluster is running incompatible operation")
    monkeypatch.setattr(runner.kube, "ensure_node_pool", resize)
    run.ensure_nodes(run.plan.level_values("clusters", 16))
    run.settings["default_pool_pods"] = 0
    with pytest.raises(kube.CommandError):
        run.ensure_nodes(run.plan.level_values("clusters", 16))


def test_trigger_jobs_marks_every_job_in_the_profiles_home(monkeypatch):
    calls = []
    monkeypatch.setattr(operations.kube, "kubectl_json", lambda *a, **k: {"items": [{"metadata": {"name": "gw"}}]})
    monkeypatch.setattr(operations.kube, "kubectl", lambda *a, **k: calls.append(a) or "")
    assert operations.trigger_jobs("ctx", "ns", "/opt/data/profiles/platform", ("a-audit", "b-audit")) == []
    args = calls[0]
    assert args[args.index("--") + 1:args.index("--") + 3] == ("env", "HERMES_HOME=/opt/data/profiles/platform")
    assert args[-2:] == ("a-audit", "b-audit")


def test_a_job_that_cannot_be_marked_is_an_error(monkeypatch):
    def fail(*a, **k):
        raise kube.CommandError("exited 3: b-audit")
    monkeypatch.setattr(operations.kube, "kubectl_json", lambda *a, **k: {"items": [{"metadata": {"name": "gw"}}]})
    monkeypatch.setattr(operations.kube, "kubectl", fail)
    [error] = operations.trigger_jobs("ctx", "ns", "/home", ("a-audit", "b-audit"))
    assert "b-audit" in error


def test_unfinished_jobs_names_every_run_that_did_not_complete(monkeypatch):
    monkeypatch.setattr(operations.kube, "kubectl_json", lambda *a, **k: {"items": [{"metadata": {"name": "gw"}}]})
    monkeypatch.setattr(operations.kube, "kubectl", lambda *a, **k: (
        "a-audit completed 2026-10-27T13:40:21+00:00\nb-audit running None\nc-audit unclaimed None\n"))
    assert operations.unfinished_jobs("ctx", "ns", "/home", ("a-audit", "b-audit", "c-audit"), "2026-10-27T12:47:29Z") == [
        "b-audit running at the end of the window", "c-audit unclaimed at the end of the window"]


def test_a_trigger_soak_needs_jobs_and_their_home(tmp_path):
    plan = PHASE2.read_text() + (
        "  audits:\n    kind: trigger_soak\n    factors: [clusters]\n    window_seconds: 1800\n    jobs: [a-audit]\n")
    path = tmp_path / "plan.yaml"
    path.write_text(plan)
    with pytest.raises(ValueError, match="jobs_home"):
        load_plan(path, environ=ENV)
    path.write_text(plan + "    jobs_home: /opt/data/profiles/platform\n")
    op = next(o for o in load_plan(path, environ=ENV).operations if o.name == "audits")
    assert op.jobs == ("a-audit",) and op.jobs_home == "/opt/data/profiles/platform"


PHASE3 = REPO_BENCH / "scale" / "phase3.yaml"
OOBE_SCRIPT = REPO_BENCH.parent / "agents" / "chat" / "scripts" / "oobe.py"


def test_phase3_triggers_the_oobe_first_run_audits_in_a_window_of_their_own():
    import ast

    plan = load_plan(PHASE3, environ=ENV)
    assert {op.window_seconds for op in plan.operations if op.kind != "prompt"} == {1800}
    audits = next(op for op in plan.operations if op.kind == "trigger_soak")
    tree = ast.parse(OOBE_SCRIPT.read_text())
    first_run = next(
        ast.literal_eval(node.value) for node in tree.body
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "FIRST_RUN_AUDITS" for t in node.targets)
    )
    assert audits.jobs == tuple(first_run)
    # The fleet only shrinks: the largest level runs first.
    assert [s.level for s in plan.matrix() if s.factor == "clusters" and s.operation.name == "onboarding_30m"] == [
        16, 8, 6, 4]


def test_a_plan_without_a_required_setting_is_refused_at_load(tmp_path):
    text = PHASE3.read_text().replace("  fixture_pool: scale-pool\n", "")
    path = tmp_path / "plan.yaml"
    path.write_text(text)
    with pytest.raises(ValueError, match="fixture_pool"):
        load_plan(path, environ=ENV)


def test_a_level_that_cannot_be_applied_stops_the_run(monkeypatch, tmp_path):
    plan = load_plan(PHASE3, environ=ENV)
    run = runner.Runner(plan, tmp_path / "data.json")
    applied = []

    def apply_level(factor, level):
        applied.append(level)
        raise kube.CommandError("stockout")
    monkeypatch.setattr(run, "apply_level", apply_level)
    monkeypatch.setattr(run, "backfill_peaks", lambda *a, **k: None)
    monkeypatch.setattr(run, "pending", lambda specs: specs)
    monkeypatch.setattr(runner.operations.Relay, "start", lambda self: None)
    monkeypatch.setattr(runner.operations.Relay, "stop", lambda self: None)
    assert run.run([s for s in plan.matrix() if s.factor == "clusters"], force=False) is False
    # The 8-cluster level, which would delete the clusters the 16-cluster level holds, never starts.
    assert applied == [16]


def test_remonitor_replaces_only_the_monitoring_metrics(monkeypatch, tmp_path):
    path = tmp_path / "data.json"
    records.merge(path, [records.Record(
        commit="e" * 40, mode="next", phase="phase3", factor="base", level=0, operation="onboarding_30m", run=1,
        started="2026-10-27T00:00:00Z", ended="2026-10-27T00:30:00Z",
        metrics=[records.Metric("memory_peak_bytes", 1.0, "shell"), records.Metric("proxy_commands", 9.0)],
        errors=["monitoring: HTTPError: 503"])])
    run = runner.Runner(load_plan(PHASE3, environ=ENV), path)
    run.install = type("Install", (), {"container_peaks": lambda self, start, end: {
        "shell": {"memory_peak_bytes": 2.0, "cpu_peak_cores": 0.5, "cpu_seconds": 300.0}}})()
    assert run.remonitor() == 1
    stored = records.load(path)["records"][0]
    assert {(m["name"], m.get("container"), m["value"]) for m in stored["metrics"]} == {
        ("proxy_commands", None, 9.0), ("memory_peak_bytes", "shell", 2.0), ("cpu_peak_cores", "shell", 0.5),
        ("cpu_seconds", "shell", 300.0)}
    assert stored["errors"] == []
    assert stored["remonitored"] == runner.RUNNER_REVISION


def _short_run(operation, started, ended, clusters=2, metrics=(), values=None):
    return records.Record(
        commit="e" * 40, mode="next", phase="phase2", factor="base", level=0, operation=operation, run=1,
        started=started, ended=ended, totals={"clusters": clusters}, values=values or {},
        metrics=[records.Metric(name, value) for name, value in metrics])


@pytest.mark.parametrize("started,ended,clusters,shared", [
    ("2026-10-27T00:01:00Z", "2026-10-27T00:03:00Z", 2, True),    # starts inside the read (3 min at 2)
    ("2026-10-27T00:05:00Z", "2026-10-27T00:07:00Z", 2, False),   # starts after it
    ("2026-10-27T00:05:00Z", "2026-10-27T00:07:00Z", 16, True),   # the read lasts 10 min at 16
    ("2026-10-27T00:28:30Z", "2026-10-27T00:30:30Z", 2, True),    # holds the :30 tick
])
@pytest.mark.parametrize("kind", ["prompt", "restart_soak"])
def test_a_short_run_inside_a_stall_watch_read_leaves_the_resource_fits(started, ended, clusters, shared, kind):
    record = _short_run("fan_out", started, ended, clusters)
    runner.Runner.mark_shared(record, kind)
    assert ("cpu_peak_cores" in record.excluded_from) is shared
    assert ("response_seconds" in record.excluded_from) is False
    assert bool(record.errors) is shared
    runner.Runner.mark_shared(record, kind)
    assert len(record.errors) == (1 if shared else 0)


def test_a_proxy_load_run_with_commands_from_outside_leaves_the_fits():
    window = ("2026-10-27T00:10:00Z", "2026-10-27T00:13:00Z")
    # The proxy counts a command before the client sees its answer, so a clean window's counts are equal.
    clean = _short_run("proxy_load", *window, metrics=(("proxy_commands", 213.0), ("proxy_load_completed", 213.0)),
                       values={"proxy_concurrency": 16})
    runner.Runner.mark_shared(clean, "proxy_load")
    assert clean.excluded_from == []
    shared = _short_run("proxy_load", *window, metrics=(("proxy_commands", 216.0), ("proxy_load_completed", 213.0)),
                        values={"proxy_concurrency": 16})
    runner.Runner.mark_shared(shared, "proxy_load")
    assert {"proxy_load_per_second", "cpu_peak_cores", "tokens_input"} <= set(shared.excluded_from)
    assert shared.errors == ["brokered commands from outside the load ran during it: 3"]


def test_a_run_without_its_totals_also_leaves_its_per_tool_and_throttle_readings():
    window = ("2026-10-27T00:10:00Z", "2026-10-27T00:13:00Z")
    readings = (("proxy_commands", 216.0), ("proxy_load_completed", 213.0), ("proxy_commands_gcloud", 216.0),
                ("proxy_command_seconds_gcloud", 900.0), ("cpu_throttled_seconds", 40.0),
                ("cpu_throttled_period_share", 0.9), ("proxy_slots_in_use_peak", 8.0))
    # The slots peak is the shared broker's occupancy too, so it goes with the CPU totals.
    split = {"proxy_commands_gcloud", "proxy_command_seconds_gcloud", "cpu_throttled_seconds",
             "cpu_throttled_period_share", "proxy_slots_in_use_peak"}
    shared = _short_run("proxy_load_gcloud", *window, metrics=readings, values={"proxy_concurrency": 16})
    runner.Runner.mark_shared(shared, "proxy_load")
    runner.Runner.cover_split_fits(shared)
    assert split <= set(shared.excluded_from)
    # A prompt answered without its operation leaves out the totals on another path.
    unfinished = _short_run("fan_out", *window, metrics=readings)
    unfinished.excluded_from = list(runner.RESOURCE_FITS)
    runner.Runner.cover_split_fits(unfinished)
    assert split <= set(unfinished.excluded_from)
    clean = _short_run("fan_out", "2026-10-27T00:05:00Z", "2026-10-27T00:07:00Z", 2, metrics=readings)
    runner.Runner.mark_shared(clean, "prompt")
    runner.Runner.cover_split_fits(clean)
    assert clean.excluded_from == []


def test_cpu_seconds_sum_the_replicas_and_peaks_take_the_highest(monkeypatch):
    install = collect.Install("p", "ctx", "ns", "host", "zone")

    # Two replicas, one point each, ending 10 minutes into a window that starts at 00:00.
    def series(query, aligner, start, end):
        points = {"ALIGN_MAX": (100.0, 300.0), "ALIGN_RATE": (0.2, 0.4), "ALIGN_DELTA": (30.0, 60.0)}[aligner]
        return [{"resource": {"labels": {"container_name": "litellm-container"}},
                 "points": [{"value": {"doubleValue": value},
                             "interval": {"endTime": "2026-10-27T00:10:00Z"}}]} for value in points]
    monkeypatch.setattr(install, "_time_series", series)
    window_start = 1793059200.0  # 2026-10-27T00:00:00Z
    peaks = install.container_peaks(window_start, window_start + 1800.0)["litellm-container"]
    assert peaks == {"memory_peak_bytes": 300.0, "cpu_peak_cores": 0.4, "cpu_seconds": 90.0}


def test_cpu_seconds_count_only_the_first_points_share_inside_the_window(monkeypatch):
    install = collect.Install("p", "ctx", "ns", "host", "zone")

    # A first point ending 15 s into the window, then a full one.
    def series(query, aligner, start, end):
        return [{"resource": {"labels": {"container_name": "litellm-container"}},
                 "points": [{"value": {"doubleValue": 60.0}, "interval": {"endTime": "2026-10-27T00:01:15Z"}},
                            {"value": {"doubleValue": 60.0}, "interval": {"endTime": "2026-10-27T00:00:15Z"}}]}]
    monkeypatch.setattr(install, "_time_series", series)
    window_start = 1793059200.0  # 2026-10-27T00:00:00Z
    peaks = install.container_peaks(window_start, window_start + 1800.0)["litellm-container"]
    assert peaks["cpu_seconds"] == 75.0


def test_the_minute_before_a_window_is_not_its_work():
    # An aligned point ending 15 s after the window starts covers 45 s from before it.
    point = {"value": {"doubleValue": 60.0}, "interval": {"endTime": "2026-10-27T00:00:15.000000Z"}}
    assert collect._share_inside(point, 1793059200.0) == 0.25


def test_a_plan_can_run_its_base_last():
    plan = load_plan(PHASE3, environ=ENV)
    levels = list(dict.fromkeys((s.factor, s.level) for s in plan.matrix()))
    assert levels == [("clusters", 16), ("clusters", 8), ("clusters", 6), ("clusters", 4), ("base", 0)]
    assert load_plan(PHASE2, environ=ENV).matrix()[0].factor == "base"


def test_the_runner_waits_for_triggered_jobs_and_marks_runs_they_still_load(monkeypatch, tmp_path):
    run = runner.Runner(load_plan(PHASE3, environ=ENV), tmp_path / "data.json")
    run.install = type("Install", (), {"context": "ctx", "namespace": "ns"})()
    op = next(o for o in run.plan.operations if o.kind == "trigger_soak")
    answers = iter([{"a-audit": "running", "b-audit": "claimed"}, {"a-audit": "completed", "b-audit": "claimed"},
                    {"a-audit": "completed", "b-audit": "completed"}, {"a-audit": "completed", "b-audit": "completed"}])
    monkeypatch.setattr(runner.operations, "job_statuses", lambda *a: next(answers))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    run.drain_jobs(op, "2026-10-27T00:00:00Z")
    assert run.carried_work() == ([], [])
    assert run.triggered is None
    # A job that outlives the drain marks every later run until it finishes; one never marked does not.
    run.settings["trigger_drain_seconds"] = 0
    statuses = {"a-audit": "running", "b-audit": "unclaimed"}
    monkeypatch.setattr(runner.operations, "job_statuses", lambda *a: statuses)
    run.drain_jobs(op, "2026-10-27T00:00:00Z")
    for _ in range(2):
        notes, fits = run.carried_work()
        assert notes == ["a-audit, started by an earlier run, was running"]
        assert "cpu_seconds" in fits and "proxy_commands" in fits
    statuses["a-audit"] = "completed"
    assert run.carried_work() == ([], [])


def test_a_resumed_runner_checks_the_latest_trigger_soaks_jobs(tmp_path):
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"schema": 1, "records": [
        {"key": "k1", "operation": "audits_30m", "started": "2026-10-27T01:00:00Z"},
        {"key": "k2", "operation": "audits_30m", "started": "2026-10-27T03:00:00Z"},
        {"key": "k3", "operation": "fan_out", "started": "2026-10-27T04:00:00Z"},
    ]}))
    run = runner.Runner(load_plan(PHASE3, environ=ENV), data)
    op, since = run.last_trigger()
    assert (op.name, since) == ("audits_30m", "2026-10-27T03:00:00Z")

def test_an_hour_long_soak_is_not_marked_for_the_ticks_it_always_holds():
    record = _short_run("watcher_soak", "2026-10-27T00:10:00Z", "2026-10-27T01:10:00Z")
    runner.Runner.mark_shared(record, "soak")
    assert record.excluded_from == []


def test_a_pod_unread_at_the_start_adds_no_growth():
    sample = ("kubeagents_tool_execution_duration_seconds_count", {}, 500.0)
    before = collect.Snapshot(unread={"uid-1"})
    after = collect.Snapshot(scraped={"proxy": {"uid-1": [sample]}})
    assert collect._growth(before, after, "proxy", "kubeagents_tool_execution_duration_seconds_count") == 0.0
    assert collect._growth(collect.Snapshot(), after, "proxy", "kubeagents_tool_execution_duration_seconds_count") == 500.0


def test_job_seconds_are_read_for_completed_runs_only(monkeypatch):
    out = ("compliance-audit completed 2026-10-28T20:30:32.450605+00:00 2026-10-28T20:10:28.731243+00:00\n"
           "stall-watch running None 2026-10-28T20:30:28.731243+00:00\n"
           "obtainability-audit unclaimed None None\n")
    monkeypatch.setattr(operations, "_gateway_pod", lambda *a: "pod")
    monkeypatch.setattr(operations.kube, "kubectl", lambda *a, **k: out)
    durations = operations.job_seconds("ctx", "ns", "/home", ("compliance-audit", "stall-watch"), "2026-10-28T20:00:00Z")
    assert durations == {"compliance-audit": pytest.approx(1203.72, abs=0.01)}


def test_a_cluster_delete_writes_no_shared_kubeconfig(monkeypatch):
    seen = {}

    def fake_run(args, *, stdin=None, timeout=0, env=None):
        seen["kubeconfig"] = (env or {}).get("KUBECONFIG")
        return ""

    monkeypatch.setattr(kube, "run", fake_run)
    monkeypatch.setenv("KUBECONFIG", "/shared/kubeconfig")
    kube.delete_cluster("p", "z", "scale-01")
    assert seen["kubeconfig"] and seen["kubeconfig"] != "/shared/kubeconfig"


PHASE4 = REPO_BENCH / "scale" / "phase4.yaml"


def test_a_triggered_window_writes_its_job_metrics_at_once(monkeypatch, tmp_path):
    plan = load_plan(PHASE4, environ=ENV)
    run = runner.Runner(plan, tmp_path / "data.json")
    spec = next(s for s in plan.matrix() if s.factor == "clusters" and s.operation.kind == "trigger_soak")
    stamped = records.Record(commit="e" * 40, mode="next", phase="phase4", factor=spec.factor, level=spec.level,
                             operation=spec.operation.name, run=1, started="2026-10-29T00:00:00Z",
                             ended="2026-10-29T00:10:00Z")
    monkeypatch.setattr(run, "apply_level", lambda factor, level: {"clusters": spec.level})
    monkeypatch.setattr(run, "backfill_peaks", lambda *a, **k: None)
    monkeypatch.setattr(run, "pending", lambda specs: specs)
    monkeypatch.setattr(run, "preflight", lambda: None)
    monkeypatch.setattr(run, "wait_registered", lambda count: None)
    monkeypatch.setattr(run, "execute", lambda s, values: stamped)
    monkeypatch.setattr(run, "drain_jobs", lambda op, since: None)
    monkeypatch.setattr(run, "flush_peaks", lambda awaiting, wait: [])
    monkeypatch.setattr(runner.operations, "job_seconds", lambda *a: {"stall-watch": 418.5})
    monkeypatch.setattr(runner.operations.Relay, "start", lambda self: None)
    monkeypatch.setattr(runner.operations.Relay, "stop", lambda self: None)
    assert run.run([spec], force=False) is True
    [stored] = records.load(tmp_path / "data.json")["records"]
    assert {m["name"]: m["value"] for m in stored["metrics"]} == {"jobs_completed": 1.0, "job_seconds": 418.5}


@pytest.mark.parametrize("error", [kube.CommandError("exec failed"), ValueError("bad timestamp"), TypeError("None")])
def test_a_job_log_that_cannot_be_read_costs_only_the_job_metrics(monkeypatch, tmp_path, error):
    run = runner.Runner(load_plan(PHASE4, environ=ENV), tmp_path / "data.json")
    run.install = type("Install", (), {"context": "ctx", "namespace": "ns"})()
    op = next(o for o in run.plan.operations if o.kind == "trigger_soak")

    def fail(*args):
        raise error
    monkeypatch.setattr(runner.operations, "job_seconds", fail)
    assert run.job_metrics(op, "2026-10-29T00:00:00Z") == []


def test_the_dry_run_lists_each_level_s_cluster_changes_and_runs(monkeypatch, tmp_path):
    plan = load_plan(PHASE4, environ=ENV)
    run = runner.Runner(plan, tmp_path / "data.json")
    monkeypatch.setattr(runner.kube, "clusters_with_prefix", lambda *a: ["scale-01", "scale-02"])
    lines = run.dry_run([s for s in plan.matrix() if s.factor == "clusters" and s.level in (40, 32)], force=True)
    assert lines[0] == "8 runs to do (of 8); test clusters now: 2"
    assert any(line.startswith("  create 38 clusters:") for line in lines)
    assert any(line.startswith("  delete 8 clusters: scale-33") for line in lines)
    assert sum("(prompt, model calls)" in line for line in lines) == 4


def test_a_lease_held_elsewhere_refuses_the_run(monkeypatch):
    plan = load_plan(PHASE4, environ=ENV)
    calls = []

    def lease(args, **kwargs):
        calls.append(args[2])
        return subprocess.CompletedProcess(args, 1, stdout="held by someone@host", stderr="")
    monkeypatch.setattr(runner.subprocess, "run", lease)
    with pytest.raises(SystemExit, match="could not take the install.s live-test lease"):
        with runner.hold_lease(plan, "test"):
            pytest.fail("the block ran without the lease")
    assert calls == ["acquire"]


def test_a_held_lease_is_released_when_the_block_ends(monkeypatch):
    plan = load_plan(PHASE4, environ=ENV)
    calls = []

    def lease(args, **kwargs):
        calls.append(args[2])
        assert kwargs["env"]["KUBE_AGENTS_LIVE_TEST_ENVS"]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(runner.subprocess, "run", lease)
    with runner.hold_lease(plan, "test"):
        assert calls == ["acquire"]
    assert calls == ["acquire", "release"]


def test_a_run_another_phase_recorded_is_skipped_with_a_warning(monkeypatch, tmp_path, caplog):
    plan = load_plan(PHASE4, environ=ENV)
    run = runner.Runner(plan, tmp_path / "data.json")
    spec = next(s for s in plan.matrix() if s.operation.kind == "prompt" and s.factor == "clusters")
    records.merge(tmp_path / "data.json", [records.Record(
        commit="e" * 40, mode="next", phase="phase3", factor=spec.factor, level=spec.level,
        operation=spec.operation.name, run=spec.run, started="2026-10-27T00:00:00Z", ended="2026-10-27T00:05:00Z")])
    monkeypatch.setattr(runner.operations, "wait_gateway_ready", lambda *a: None)
    run.install = type("Install", (), {"context": "ctx", "namespace": "ns", "image": lambda self: "img:" + "e" * 40,
                                       "mode": lambda self: "next"})()
    with caplog.at_level("WARNING"):
        assert spec not in run.pending([spec])
    assert "already recorded by phase3" in caplog.text


def test_every_test_cluster_s_credentials_are_refreshed(monkeypatch, tmp_path):
    run = runner.Runner(load_plan(PHASE4, environ=ENV), tmp_path / "data.json")
    fetched = []
    monkeypatch.setattr(runner.kube, "clusters_with_prefix", lambda *a: ["scale-01", "scale-02"])
    monkeypatch.setattr(runner.kube, "has_context", lambda name: True)
    monkeypatch.setattr(runner.kube, "get_credentials", lambda project, zone, name: fetched.append(name))
    monkeypatch.setattr(run, "wait_registered", lambda count: None)
    run.ensure_clusters(2)
    # A cluster created again under an old name keeps no stale endpoint.
    assert fetched == ["scale-01", "scale-02"]


def test_kubectl_requests_time_out_except_streaming_ones(monkeypatch):
    calls = []
    monkeypatch.setattr(kube, "run", lambda args, **kwargs: calls.append(args) or "")
    kube.kubectl("ctx", "get", "nodes", timeout=60)
    kube.kubectl("ctx", "exec", "pod", "--", "true", timeout=60)
    assert "--request-timeout=60s" in calls[0]
    assert not any(arg.startswith("--request-timeout") for arg in calls[1])


PROXY_TEXT = """kubeagents_tool_execution_duration_seconds_count{{tool="kubectl"}} {kubectl}
kubeagents_tool_execution_duration_seconds_count{{tool="gcloud"}} {gcloud}
kubeagents_tool_execution_duration_seconds_sum{{tool="kubectl"}} {kubectl_s}
kubeagents_tool_execution_duration_seconds_sum{{tool="gcloud"}} {gcloud_s}
kubeagents_credential_proxy_slot_cap 8
"""


def _proxy(pod: str, kubectl: int, gcloud: int, kubectl_s: float, gcloud_s: float) -> dict[str, list]:
    text = PROXY_TEXT.format(kubectl=kubectl, gcloud=gcloud, kubectl_s=kubectl_s, gcloud_s=gcloud_s)
    return {pod: collect.parse_prometheus(text)}


def test_proxy_commands_are_split_by_tool_and_the_cap_is_read():
    before = Snapshot(scraped={"proxy": _proxy("p", 10, 4, 20.0, 40.0)})
    after = Snapshot(scraped={"proxy": _proxy("p", 25, 6, 50.0, 70.0)})
    metrics = {(m.name, m.container): m.value for m in collect.counter_metrics(before, after)}
    assert metrics[("proxy_commands", None)] == 17
    assert metrics[("proxy_commands_kubectl", None)] == 15
    assert metrics[("proxy_commands_gcloud", None)] == 2
    assert metrics[("proxy_command_seconds_kubectl", None)] == 30.0
    assert metrics[("proxy_command_seconds_gcloud", None)] == 30.0
    assert metrics[("proxy_slot_cap", None)] == 8


def test_a_new_proxy_pod_counts_its_tools_from_zero():
    before = Snapshot(scraped={"proxy": _proxy("old", 100, 50, 1.0, 1.0)})
    after = Snapshot(scraped={"proxy": _proxy("new", 3, 1, 1.0, 1.0)})
    metrics = {(m.name, m.container): m.value for m in collect.counter_metrics(before, after)}
    assert metrics[("proxy_commands_kubectl", None)] == 3
    assert metrics[("proxy_commands_gcloud", None)] == 1


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("usage_usec 9\nnr_periods 100\nnr_throttled 25\nthrottled_usec 2500000\n", 2.5),
        ("nr_periods 100\nnr_throttled 25\nthrottled_time 2500000000\n", 2.5),
    ],
)
def test_parse_cpu_stat_reads_cgroup_v2_and_v1(text, seconds):
    stat = collect.parse_cpu_stat(text)
    assert stat == {"nr_periods": 100.0, "nr_throttled": 25.0, "throttled_seconds": seconds}


def test_throttle_metrics_are_the_window_growth():
    before = Snapshot(cpu_stat={"p": {"nr_periods": 100.0, "nr_throttled": 10.0, "throttled_seconds": 1.0}})
    after = Snapshot(cpu_stat={"p": {"nr_periods": 300.0, "nr_throttled": 110.0, "throttled_seconds": 6.0}})
    metrics = {(m.name, m.container): m.value for m in collect.throttle_metrics(before, after)}
    assert metrics[("cpu_throttled_seconds", "envoy-credential-proxy")] == 5.0
    assert metrics[("cpu_throttled_period_share", "envoy-credential-proxy")] == 0.5


def test_no_cpu_stat_means_no_throttle_metrics():
    assert collect.throttle_metrics(Snapshot(), Snapshot()) == []


class _NoPrometheusInstall(collect.Install):
    """Monitoring with container series but no Managed Prometheus metric."""

    def _time_series(self, query, aligner, start, end):
        if collect.PROXY_SLOTS_METRIC in query:
            raise collect.urllib.error.HTTPError("url", 404, "Cannot find metric(s)", None, None)
        if aligner == "ALIGN_MAX":
            return [{"resource": {"labels": {"container_name": "envoy-credential-proxy"}},
                     "points": [{"value": {"int64Value": "734003200"}}]}]
        return []


def test_a_missing_slots_metric_keeps_the_container_peaks():
    install = _NoPrometheusInstall("p", "ctx", "ns", "scale-host", "us-central1-c")
    peaks = install.container_peaks(0.0, 60.0)
    assert peaks["envoy-credential-proxy"]["memory_peak_bytes"] == 734003200
    assert "proxy_slots_in_use_peak" not in peaks["envoy-credential-proxy"]


def test_remonitor_keeps_one_slots_peak_and_notes_a_missing_one(tmp_path):
    path = tmp_path / "data.json"
    records.merge(path, [records.Record(
        commit="e" * 40, mode="next", phase="broker", factor="base", level=0, operation="audits_30m", run=1,
        started="2026-10-28T20:00:00Z", ended="2026-10-28T20:30:00Z",
        metrics=[records.Metric("proxy_slot_cap", 8.0), records.Metric("proxy_slots_in_use_peak", 3.0, "envoy-credential-proxy")])])
    run = runner.Runner(load_plan(PHASE3, environ=ENV), path)
    run.install = type("Install", (), {"container_peaks": lambda self, start, end: {
        "envoy-credential-proxy": {"memory_peak_bytes": 2.0, "proxy_slots_in_use_peak": 5.0}}})()
    run.remonitor()
    run.remonitor()
    stored = records.load(path)["records"][0]
    slots = [m["value"] for m in stored["metrics"] if m["name"] == "proxy_slots_in_use_peak"]
    assert slots == [5.0]
    assert runner.SLOTS_UNKNOWN_NOTE not in stored["errors"]
    run.install = type("Install", (), {"container_peaks": lambda self, start, end: {
        "envoy-credential-proxy": {"memory_peak_bytes": 2.0}}})()
    run.remonitor()
    stored = records.load(path)["records"][0]
    assert stored["errors"] == [runner.SLOTS_UNKNOWN_NOTE]


def test_a_record_from_an_older_runner_gets_no_slots_note():
    record = records.Record(commit="e" * 40, mode="next", phase="phase3", factor="base", level=0,
                            operation="audits_30m", run=1, started="2026-10-27T00:00:00Z",
                            ended="2026-10-27T00:30:00Z", metrics=[records.Metric("proxy_commands", 9.0)])
    runner.Runner.note_slots(record)
    assert record.errors == []


def _scrubber(project="my-project-123"):
    run = object.__new__(runner.Runner)
    run.project = project
    return run


@pytest.mark.parametrize("text,expected", [
    ("image us-central1-docker.pkg.dev/my-project-123/repo/agent:abc", "image us-central1-docker.pkg.dev/<project>/repo/agent:abc"),
    # Only an answer preview is cut, so an error or the image keeps its end.
    ("context gke_my-proj", "context gke_my-proj"),
])
def test_scrub_removes_the_whole_project(text, expected):
    assert _scrubber().scrub(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("in my-project-123 and cut inside the ID my-proj", "in <project> and cut inside the ID <project>"),
    ("running in the project `my", "running in the project `<project>"),
    ("under project m", "under project <project>"),
    ("under the `my-pr", "under the `<project>"),
    ("ends with my-p", "ends with my-p"),
    ("as per the install's `m", "as per the install's `m"),
    ("a word that ends xmy-proj", "a word that ends xmy-proj"),
])
def test_scrub_answer_removes_a_cut_off_start_of_the_project(text, expected):
    assert _scrubber().scrub_answer(text) == expected


@pytest.mark.parametrize("tail,expected_end", [
    ("under the project `my-project-123`.", "under the project"),
    ("under the project `my", "under the project `my"),
])
def test_an_answer_preview_never_ends_inside_a_word(tail, expected_end):
    # The tail starts so that the bound falls inside the ID in the first case.
    head = "x " * ((operations.ANSWER_PREVIEW_CHARS - len("under the project `my")) // 2)
    output = head[: operations.ANSWER_PREVIEW_CHARS - len("under the project `my")] + tail
    preview = operations.answer_preview(output)
    assert len(preview) <= operations.ANSWER_PREVIEW_CHARS
    assert preview.rstrip().endswith(expected_end)


def test_a_new_preview_keeps_a_complete_last_word_that_starts_the_project():
    # A new preview never ends inside a word, so only the whole ID is scrubbed: the cut-off
    # scrub, for stored records, would rewrite this complete word.
    run = _scrubber()
    assert run.fresh_answers(["in my-project-123, owned by my-proj"]) == ["in <project>, owned by my-proj"]
    assert run.scrub_answer("owned by my-proj") == "owned by <project>"


def test_an_answer_preview_with_no_space_keeps_the_hard_cut():
    # A long URL or compact JSON has no word to cut before; an empty preview would read as a
    # failed prompt.
    output = "https://example.com/" + "x" * 400
    assert operations.answer_preview(output) == output[: operations.ANSWER_PREVIEW_CHARS]


def test_scrub_answer_leaves_the_earlier_phases_answers_unchanged():
    # A project ID that starts with the same letter as one of their cut-off previews.
    run = _scrubber("f-sample-project")
    data = json.loads((REPO_BENCH / "scale" / "harness-limits.json").read_text())
    answers = [a for r in data["records"] if not r["phase"].startswith("broker") for a in r.get("answers", [])]
    assert answers
    assert [run.scrub_answer(a) for a in answers] == answers


class _SlotsInstall(collect.Install):
    def __init__(self, slots=None, error=None):
        super().__init__("p", "ctx", "ns", "scale-host", "us-central1-c")
        self.slots, self.error = slots, error

    def _time_series(self, query, aligner, start, end):
        if collect.PROXY_SLOTS_METRIC in query:
            if self.error:
                raise collect.urllib.error.HTTPError("url", self.error, "error", None, None)
            return [{"resource": {"labels": {}}, "points": [{"value": {"doubleValue": v}} for v in self.slots]}]
        return []


def test_a_slots_series_becomes_the_proxy_slots_peak():
    peaks = _SlotsInstall(slots=[3.0, 5.0]).container_peaks(0.0, 60.0)
    assert peaks["envoy-credential-proxy"]["proxy_slots_in_use_peak"] == 5.0


class _FailingSlotsInstall(_NoPrometheusInstall):
    """Container series, and a slots query that fails with a status other than 404."""

    def _time_series(self, query, aligner, start, end):
        if collect.PROXY_SLOTS_METRIC in query:
            raise collect.urllib.error.HTTPError("url", 503, "Service Unavailable", None, None)
        return super()._time_series(query, aligner, start, end)


def test_a_failed_slots_query_keeps_the_container_peaks_and_says_why(tmp_path):
    install = _FailingSlotsInstall("p", "ctx", "ns", "scale-host", "us-central1-c")
    peaks = install.container_peaks(0.0, 60.0)
    assert peaks["envoy-credential-proxy"]["memory_peak_bytes"] == 734003200
    assert "proxy_slots_in_use_peak" not in peaks["envoy-credential-proxy"]
    assert install.slots_query_error.startswith("HTTPError: HTTP Error 503")
    # The record keeps the peaks and notes the failed query, which is no monitoring error.
    path = tmp_path / "data.json"
    records.merge(path, [records.Record(
        commit="e" * 40, mode="next", phase="broker", factor="base", level=0, operation="audits_30m", run=1,
        started="2026-10-28T20:00:00Z", ended="2026-10-28T20:30:00Z",
        metrics=[records.Metric("proxy_slot_cap", 8.0)])])
    run = runner.Runner(load_plan(PHASE3, environ=ENV), path)
    run.install = install
    run.remonitor()
    stored = records.load(path)["records"][0]
    assert any(m["name"] == "memory_peak_bytes" for m in stored["metrics"])
    [note] = stored["errors"]
    assert note.startswith(runner.SLOTS_FAILED_NOTE) and "503" in note
    assert not note.startswith(runner.MONITORING_ERROR)
    # A later read that gets an answer drops the note.
    run.install = _NoPrometheusInstall("p", "ctx", "ns", "scale-host", "us-central1-c")
    run.remonitor()
    assert records.load(path)["records"][0]["errors"] == [runner.SLOTS_UNKNOWN_NOTE]


def test_a_pod_whose_cpu_stat_was_unread_at_the_start_adds_no_throttle():
    before = Snapshot(cpu_unread={"p"})
    after = Snapshot(cpu_stat={"p": {"nr_periods": 1e6, "nr_throttled": 4e5, "throttled_seconds": 9000.0}})
    assert collect.throttle_metrics(before, after) == []


BROKER_RAISED = REPO_BENCH / "scale" / "broker-proxy-raised.yaml"


def _raised_runner(tmp_path, proxy_limits):
    plan = load_plan(BROKER_RAISED, environ=ENV)
    run = runner.Runner(plan, tmp_path / "data.json")
    read = []

    def limits():
        read.append(True)
        return {collect.PROXY_CONTAINER: proxy_limits}
    run.install = types.SimpleNamespace(limits=limits)
    return plan, run, read


def test_a_run_starts_when_the_broker_carries_the_plan_s_limits(monkeypatch, tmp_path):
    plan, run, read = _raised_runner(tmp_path, {"cpu_cores": 2.0, "memory_bytes": 2 * 1024**3})
    started = []
    monkeypatch.setattr(run, "run", lambda *a: started.append(True) or True)
    args = types.SimpleNamespace(command="run", force=False, limit=None)
    assert runner._mutate(run, args, []) == 0
    assert started and read
    monkeypatch.setattr(runner.kube, "clusters_with_prefix", lambda *a: [])
    assert "expect_proxy_limits: the broker matches" in run.dry_run([], force=True)


def test_a_run_refuses_to_start_while_the_broker_carries_other_limits(monkeypatch, tmp_path):
    plan, run, read = _raised_runner(tmp_path, {"cpu_cores": 1.0})
    started = []
    monkeypatch.setattr(run, "run", lambda *a: started.append(True) or True)
    for command in ("run", "apply"):
        args = types.SimpleNamespace(command=command, force=False, limit=None, factor="clusters", level=8)
        with pytest.raises(SystemExit, match="cpu: the plan expects 2, the broker has 1"):
            runner._mutate(run, args, [])
    assert started == []
    monkeypatch.setattr(runner.kube, "clusters_with_prefix", lambda *a: [])
    assert any("expect_proxy_limits: cpu: the plan expects 2" in line for line in run.dry_run([], force=True))


def test_a_plan_that_names_no_limits_reads_none(tmp_path):
    plan = load_plan(PHASE4, environ=ENV)
    run = runner.Runner(plan, tmp_path / "data.json")
    run.install = types.SimpleNamespace(limits=lambda: pytest.fail("a plan with no expect_proxy_limits read the limits"))
    run.check_limits()
    assert run.limit_mismatches() == []
