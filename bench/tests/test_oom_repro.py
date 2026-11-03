"""Offline tests for the OOM reproduction harness: the fixtures it applies and how it reads a kill."""
import json

import pytest
import yaml

from kube_agents_bench.oom_repro import capture, cli, fixtures


def test_node_is_kwok_managed_tainted_and_spread():
    nodes = [fixtures.node(i) for i in range(6)]
    first = nodes[0]
    assert first["metadata"]["annotations"] == {"kwok.x-k8s.io/node": "fake"}
    assert first["spec"]["taints"] == [{"key": "kwok.x-k8s.io/node", "value": "fake", "effect": "NoSchedule"}]
    assert first["metadata"]["labels"]["type"] == "kwok"
    assert {n["metadata"]["labels"]["topology.kubernetes.io/zone"] for n in nodes} == set(fixtures.ZONES)
    assert {n["metadata"]["labels"]["cloud.google.com/gke-nodepool"] for n in nodes} == set(fixtures.NODE_POOLS)
    assert len({n["metadata"]["name"] for n in nodes}) == 6


def test_workloads_put_namespaces_first_and_schedule_only_on_kwok_nodes():
    docs = fixtures.workloads(namespaces=2, per_namespace=3, replicas=30)
    kinds = [d["kind"] for d in docs]
    assert kinds[:2] == ["Namespace", "Namespace"]
    assert kinds.count("Deployment") == 6 and kinds.count("Service") == 6
    pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"type": "kwok"}
    assert pod["tolerations"] == [{"key": "kwok.x-k8s.io/node", "operator": "Exists", "effect": "NoSchedule"}]
    assert next(d for d in docs if d["kind"] == "Deployment")["spec"]["replicas"] == 30


def test_event_is_a_warning_in_a_workload_namespace_with_a_unique_name():
    names = set()
    for i in range(40):
        ns, body = fixtures.event(i, namespaces=150, per_namespace=10, nodes=1500, run_id="00001",
                                  now="2026-11-03T00:00:00Z")
        assert ns.startswith("team-") and body["metadata"]["namespace"] == ns
        assert body["type"] == "Warning" and body["involvedObject"]["namespace"] == ns
        assert body["reason"] in {r for r, _, _ in fixtures.EVENT_REASONS}
        names.add(body["metadata"]["name"])
    assert len(names) == 40
    _, scheduling = fixtures.event(0, 150, 10, 1500, "00001", "2026-11-03T00:00:00Z")
    assert "0/1500 nodes are available" in scheduling["message"]


def test_event_padding_lengthens_the_message_only():
    _, short = fixtures.event(1, 150, 10, 1500, "00001", "t")
    _, padded = fixtures.event(1, 150, 10, 1500, "00001", "t", pad_to=1024)
    assert len(padded["message"]) == 1024 and padded["message"].startswith(short["message"])
    _, unpadded = fixtures.event(1, 150, 10, 1500, "00001", "t", pad_to=10)
    assert unpadded["message"] == short["message"]


def test_kwok_manifest_drops_the_flowschema_gke_refuses():
    release = yaml.safe_dump_all([
        {"kind": "Deployment", "metadata": {"name": "kwok-controller"}},
        {"kind": "FlowSchema", "metadata": {"name": "kwok-controller"}},
        None,
    ])
    kept = [d["kind"] for d in yaml.safe_load_all(fixtures.kwok_manifest(release)) if d]
    assert kept == ["Deployment"]


def test_kwok_config_raises_parallelism_and_widens_the_pod_cidr():
    config_map = {"metadata": {"name": "kwok", "namespace": "kube-system", "resourceVersion": "9"},
                  "data": {"kwok.yaml": "kind: KwokConfiguration\noptions:\n  nodeLeaseParallelism: 4\n"
                                        "  cidr: 10.0.0.1/24\n  nodeLeaseDurationSeconds: 40\n"}}
    out = fixtures.kwok_config(config_map)
    options = yaml.safe_load(out["data"]["kwok.yaml"])["options"]
    assert options["nodeLeaseParallelism"] == 32 and options["podPlayStageParallelism"] == 32
    assert options["cidr"] == "100.64.0.1/10" and options["nodeLeaseDurationSeconds"] == 40
    assert "resourceVersion" not in out["metadata"]


def test_free_blocks_skip_used_ranges_and_refuse_when_short():
    subnets = [{"ipCidrRange": "10.100.0.0/24", "secondaryIpRanges": [{"ipCidrRange": "10.101.64.0/18"}]},
               {"ipCidrRange": "10.128.0.0/20"}]
    assert fixtures.free_blocks(subnets, 2) == [102, 103]
    with pytest.raises(ValueError):
        fixtures.free_blocks(subnets, 100)


def test_chunks_deal_every_document_once():
    docs = [{"i": i} for i in range(23)]
    parts = fixtures.chunks(docs, 10)
    assert len(parts) == 10
    seen = sorted(json.loads(line)["i"] for p in parts for line in p.splitlines() if line.startswith("{"))
    assert seen == list(range(23))


def pod(name, init=(), containers=()):
    return {"metadata": {"name": name}, "status": {"initContainerStatuses": list(init),
                                                   "containerStatuses": list(containers)}}


def test_kills_reads_oomkilled_from_init_sidecars_and_containers():
    oom = {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-11-03T18:04:12Z"}
    p = pod("gw", init=[{"name": "agent-api-auth", "restartCount": 1, "lastState": {"terminated": oom}}],
            containers=[{"name": "platform-agent", "restartCount": 0, "lastState": {}},
                        {"name": "fluent-bit", "restartCount": 2, "lastState": {"terminated": {"reason": "Error"}}}])
    assert capture.kills(p) == [{"pod": "gw", "container": "agent-api-auth", "restarts": 1, "exit_code": 137,
                                 "finished": "2026-11-03T18:04:12Z"}]


def sample(ts, cg_mib, procs):
    return {"ts": ts, "cg_current": str(cg_mib * 2**20), "procs": procs}


def test_sample_at_kill_takes_the_killed_pods_last_sample_and_allows_second_rounding():
    stream = [sample(100.2, 1500, []), sample(102.2, 1900, []), sample(104.6, 2000, [])]
    for x in stream:
        x["pod"] = "proxy-a"
    other = sample(104.9, 10, [])
    other["pod"] = "proxy-b"
    # finishedAt 104 is rounded down from a kill just after the 104.6 sample.
    assert capture.sample_at_kill(stream + [other], "proxy-a", 104.0)["ts"] == 104.6
    assert capture.sample_at_kill(stream, "proxy-a", 102.5)["ts"] == 102.2
    assert capture.sample_at_kill(stream, "proxy-b", 104.0) is None
    assert capture.sample_at_kill(stream, "proxy-a", 200.0) is None  # too old to describe the kill


def test_breakdown_shares_add_to_100_and_group_kubectl_by_what_it_lists():
    procs = [[1, 0, 1000 * 1024, "/usr/bin/kubectl --request-timeout=30s get deploy,sts,ds,cronjob,pod -A -o json"],
             [2, 0, 950 * 1024, "/usr/bin/kubectl --request-timeout=30s get deploy,sts,ds,cronjob,pod -A -o json"],
             [3, 0, 90 * 1024, "/usr/local/bin/envoy --config-path /tmp/envoy.yaml"],
             [4, 0, 70 * 1024, "/opt/hermes/.venv/bin/python3 /opt/defaults/scripts/credential_proxy.py"],
             [5, 0, 72 * 1024, "/usr/bin/../lib/google-cloud-sdk/platform/bundledpythonunix/bin/python3 gcloud.py logging read"]]
    rows = capture.breakdown(sample(1, 2039, procs))
    assert rows[0][0] == "kubectl get deploy,sts,ds,cronjob,pod -A" and rows[0][2] == 1950
    assert sum(share for _, share, _ in rows) == pytest.approx(100)
    assert {name for name, _, _ in rows} == {"kubectl get deploy,sts,ds,cronjob,pod -A", "envoy",
                                             "credential_proxy.py", "gcloud"}


def test_process_class_names_the_watcher_and_tells_the_auth_plugin_from_gcloud():
    assert capture.process_class("/usr/local/bin/k8s-event-watcher --cluster-name=x") == "k8s-event-watcher"
    assert capture.process_class("/usr/lib/google-cloud-sdk/bin/gke-gcloud-auth-plugin") == "gke-gcloud-auth-plugin"
    assert capture.process_class("/usr/bin/../lib/google-cloud-sdk/lib/gcloud.py logging read") == "gcloud"
    assert capture.process_class("/opt/hermes/.venv/bin/python3 /opt/defaults/scripts/credential_proxy.py") == \
        "credential_proxy.py"


def test_render_report_breaks_down_every_kill_including_the_last():
    def at(ts, cg, procs):
        x = sample(ts, cg, procs)
        x["pod"], x["sampler_rss_kib"] = "proxy-a", 12 * 1024
        return x
    big = [[1, 0, 1800 * 1024, "/usr/bin/kubectl get deploy,sts,ds,cronjob,pod -A -o json"],
           [2, 0, 90 * 1024, "/usr/local/bin/envoy"]]
    from datetime import datetime
    epoch = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    # Each kill just after a sample, with finishedAt rounded down below the sample's time.
    samples = [at(epoch("2026-11-03T20:40:40Z") + 0.6, 1950, big), at(epoch("2026-11-03T20:43:07Z") + 0.1, 1960, big)]
    kills = [{"pod": "proxy-a", "container": "envoy-credential-proxy", "restarts": 1, "exit_code": 137,
              "finished": "2026-11-03T20:40:40Z"},
             {"pod": "proxy-a", "container": "envoy-credential-proxy", "restarts": 2, "exit_code": 137,
              "finished": "2026-11-03T20:43:07Z"}]
    text = cli.render_report("envoy-credential-proxy", kills, samples)
    assert text.count("| Share | MiB | Process |") == 2
    assert "No sample" not in text and "sampler 12 MiB" in text
    assert "`kubectl get deploy,sts,ds,cronjob,pod -A`" in text


def test_read_samples_skips_a_cut_last_line(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"ts": 1, "procs": []}\n{"ts": 2, "pro')
    assert [x["ts"] for x in cli.read_samples(path)] == [1]


def test_sampler_skips_itself_and_reports_its_own_rss():
    assert "pid == me" in capture.SAMPLER_SCRIPT and "sampler_rss_kib" in capture.SAMPLER_SCRIPT
    compile(capture.SAMPLER_SCRIPT, "sampler", "exec")


def test_defaults_and_names_match_the_operator_source():
    from pathlib import Path
    import re
    root = Path(__file__).resolve().parents[2] / "k8s-operator" / "internal" / "controller"
    proxy = (root / "credential_proxy_manifests.go").read_text()
    auth = (root / "platformagent_manifests.go").read_text()
    shell = (root / "shell_sandbox_manifests.go").read_text()
    const = lambda src, name: re.search(rf'\b{name}\s*=\s*"([^"]+)"', src).group(1)
    assert cli.DEFAULT_LIMITS["credentialProxy"] == {"cpu": const(proxy, "credentialProxyCPULimit"),
                                                     "memory": const(proxy, "credentialProxyMemoryLimit")}
    assert cli.DEFAULT_LIMITS["agentAPIAuth"] == {"cpu": const(auth, "agentAPIAuthCPULimit"),
                                                  "memory": const(auth, "agentAPIAuthMemoryLimit")}
    assert cli.DEFAULT_LIMITS["shellSandbox"] == {"cpu": const(shell, "shellSandboxCPULimit"),
                                                  "memory": const(shell, "shellSandboxMemoryLimit")}
    assert cli.PROXY_CONTAINER == const(proxy, "credentialProxyContainerName")
    assert cli.WATCHER_CONTAINER == const(auth, "agentAPIAuthContainerName")
    assert 'agent.Name + "-shell"' in shell and f'Name:  "{cli.SHELL_CONTAINER}",' in shell
    assert 'agent.Name + "-gateway"' in proxy and cli.GATEWAY_SELECTOR == f"app={cli.PLATFORM_AGENT}-gateway"
    assert cli.SHELL_SELECTOR == f"app={cli.PLATFORM_AGENT}-shell"
    assert 'json:"shellSandbox,omitempty"' in (root.parents[1] / "api" / "v1alpha1" / "common_types.go").read_text()


def test_trigger_script_calls_oobe_audit_holds():
    assert "oobe.audit_holds" in cli.JOB_TRIGGER_SCRIPT
    compile(cli.JOB_TRIGGER_SCRIPT, "trigger", "exec")


def test_resources_patch_creates_missing_parents_and_keeps_requests_under_the_limit():
    proxy, shell = cli.RESOURCE_PATHS["credentialProxy"], cli.RESOURCE_PATHS["shellSandbox"]
    assert cli.resources_patch({}, proxy, {"memory": "2Gi"}) == [
        {"op": "add", "path": "/spec/deployment", "value": {"credentialProxy": {"resources": {"limits": {"memory": "2Gi"}}}}}]
    assert cli.resources_patch({"harness": {"projectId": "p"}}, shell, {"memory": "2Gi"}) == [
        {"op": "add", "path": "/spec/harness/experimental",
         "value": {"shellSandbox": {"resources": {"limits": {"memory": "2Gi"}}}}}]
    spec = {"deployment": {"credentialProxy": {"resources": {"limits": {"memory": "480G"},
                                                             "requests": {"memory": "4Gi", "cpu": "500m"}}}}}
    assert cli.resources_patch(spec, proxy, {"cpu": "2", "memory": "2Gi"}) == [
        {"op": "add", "path": "/spec/deployment/credentialProxy/resources",
         "value": {"limits": {"cpu": "2", "memory": "2Gi"}, "requests": {"memory": "2Gi", "cpu": "500m"}}}]
    assert spec["deployment"]["credentialProxy"]["resources"]["requests"]["memory"] == "4Gi"  # input untouched


def test_quantity_compares_cpu_and_memory():
    assert cli.quantity("500m") == 0.5 and cli.quantity("2") == 2
    assert cli.quantity("2Gi") == 2 * 2**30 and cli.quantity("8G") == 8e9
    assert cli.quantity("8G") < cli.quantity("8Gi")
    with pytest.raises(ValueError):
        cli.quantity("lots")


def test_region_of_a_zone():
    assert cli.region("us-central1-f") == "us-central1"
    assert cli.region("europe-west1-b") == "europe-west1"


def test_has_limits_reads_init_sidecars_and_normalises_quantities():
    pod = {"spec": {"containers": [{"name": "platform-agent", "resources": {"limits": {"memory": "16G"}}}],
                    "initContainers": [{"name": "agent-api-auth",
                                        "resources": {"limits": {"cpu": "1000m", "memory": "2048Mi"}}}]}}
    assert cli.has_limits(pod, "agent-api-auth", {"cpu": "1", "memory": "2Gi"})
    assert not cli.has_limits(pod, "agent-api-auth", {"cpu": "2", "memory": "2Gi"})
    assert not cli.has_limits(pod, "envoy-credential-proxy", {"cpu": "1"})


def test_pool_refuses_an_empty_fleet():
    with pytest.raises(SystemExit):
        cli.pool(0)


def test_restore_patch_puts_back_resources_and_runtime_class_or_removes_what_was_absent():
    before = {"deployment": {"agentAPIAuth": {"resources": {"limits": {"memory": "4G"}}},
                             "availability": {"runtimeClassName": "gvisor"}}}
    auth, proxy = cli.RESOURCE_PATHS["agentAPIAuth"], cli.RESOURCE_PATHS["credentialProxy"]
    assert cli.restore_patch(before, before, auth, runtime_class=True) == [
        {"op": "add", "path": "/spec/deployment/agentAPIAuth/resources", "value": {"limits": {"memory": "4G"}}},
        {"op": "add", "path": "/spec/deployment/availability/runtimeClassName", "value": "gvisor"}]
    written = {"deployment": {"credentialProxy": {"resources": {"limits": {"memory": "2Gi"}}}}}
    assert cli.restore_patch({"deployment": {}}, written, proxy, runtime_class=False) == [
        {"op": "remove", "path": "/spec/deployment/credentialProxy"}]
    assert cli.restore_patch({"deployment": {}}, {"deployment": {}}, proxy, runtime_class=False) == []
    shell = cli.RESOURCE_PATHS["shellSandbox"]
    shell_before = {"harness": {"experimental": {"shellSandbox": {"resources": {"limits": {"memory": "8G"}}}}}}
    assert cli.restore_patch(shell_before, {}, shell, runtime_class=False) == [
        {"op": "add", "path": "/spec/harness/experimental/shellSandbox/resources", "value": {"limits": {"memory": "8G"}}}]


def test_proxy_watches_the_shell_by_default_and_sets_its_limits(tmp_path):
    assert cli.parse(["--host-context", "c", "--out", str(tmp_path), "proxy"]).watch == "shell"
    selector, container, field, python = cli.WATCH_TARGETS["shell"]
    assert (selector, container, field) == ("app=platform-agent-shell", "shell", "shellSandbox")
    assert cli.parse(["--host-context", "c", "--out", str(tmp_path), "proxy", "--watch", "proxy"]).watch == "proxy"


def test_process_class_names_scripts_run_by_the_agent_venv_and_skips_kubectl_flags():
    assert capture.process_class("/opt/hermes/.venv/bin/python3 /opt/data/scripts/collect.py x") == "collect.py"
    assert capture.process_class("/opt/hermes/.venv/bin/python3 /opt/hermes/.venv/bin/hermes gateway run") == "hermes"
    assert capture.process_class("/usr/bin/kubectl --kubeconfig /x get -A pods -o json") == "kubectl get pods -A"
    assert capture.process_class("kubectl get -n kube-system pods -o json") == "kubectl get pods"
    assert capture.process_class("kubectl get --context gke_p_z_a pods --all-namespaces") == "kubectl get pods -A"
    assert capture.process_class("/opt/hermes/.venv/bin/python3 -c 'print(1)'") == "other"
    assert capture.process_class("kubectl --kubeconfig /x apply -f -") == "kubectl apply"
    assert capture.process_class("kubectl --context a logs p") == "kubectl logs p"[:12]
    assert capture.process_class("kubectl get --sort-by .metadata.name pods -A") == "kubectl get pods -A"
    assert capture.process_class("kubectl get pods --all-namespaces=true") == "kubectl get pods -A"
    assert capture.process_class("kubectl get pods --namespace=x") == "kubectl get pods"


def test_process_class_names_python_scripts():
    assert capture.process_class(
        "python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/collect.py compliance-audit --workspace w"
    ) == "collect.py"
    assert capture.process_class("python3 -I -u /x/fleet_upgrade_report.py --cluster a") == "fleet_upgrade_report.py"
    assert capture.process_class("/usr/sbin/sshd -D") == "other"


def test_render_report_states_the_samples_it_took():
    assert "Samples: 0 from 0 pod(s) of `shell`: none." in cli.render_report("shell", [], [])


def test_restore_patch_removes_the_object_the_scenario_created():
    shell = cli.RESOURCE_PATHS["shellSandbox"]
    before = {"harness": {"experimental": {"platformFrontDoor": False}}}
    written = {"harness": {"experimental": {"platformFrontDoor": False,
                                            "shellSandbox": {"resources": {"limits": {"memory": "2Gi"}}}}}}
    assert cli.restore_patch(before, written, shell, runtime_class=False) == [
        {"op": "remove", "path": "/spec/harness/experimental/shellSandbox"}]
    kept = {"harness": {"experimental": {"shellSandbox": {"image": "x"}}}}
    kept_written = {"harness": {"experimental": {"shellSandbox": {"image": "x", "resources": {"limits": {}}}}}}
    assert cli.restore_patch(kept, kept_written, shell, runtime_class=False) == [
        {"op": "remove", "path": "/spec/harness/experimental/shellSandbox/resources"}]


class FakeHost:
    def __init__(self, fail_start=False):
        self.patches, self.fail_start = [], fail_start
        self.context, self.namespace = "c", "n"

    def platform_agent(self):
        return {"spec": {"deployment": {"credentialProxy": {"resources": {"limits": {"memory": "8G"}}}}}}

    def patch(self, ops):
        self.patches.append(ops)


class FakeRecorder:
    def __init__(self, *a, **k):
        self.events, self.container = [], "shell"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


REPORTS = []


def run_scenario(monkeypatch, tmp_path, host, lost=False, start=None):
    import contextlib
    import threading
    monkeypatch.setattr(cli, "Host", lambda args: host)
    monkeypatch.setattr(cli, "Recorder", FakeRecorder)
    monkeypatch.setattr(cli, "report", lambda out, recorder: REPORTS.append(out))
    event = threading.Event()
    if lost:
        event.set()

    @contextlib.contextmanager
    def fake_lease(h, note):
        yield event

    monkeypatch.setattr(cli, "lease", fake_lease)
    monkeypatch.setattr(cli, "KILL_POLL_SECONDS", 0)
    args = cli.parse(["--host-context", "c", "--out", str(tmp_path), "proxy", "--watch", "proxy", "--seconds", "0"])
    cli.scenario(args, "credentialProxy", "envoy-credential-proxy", "app=x", start or (lambda h, b, l: None))
    return host.patches


def test_scenario_sets_then_restores_the_limits(monkeypatch, tmp_path):
    patches = run_scenario(monkeypatch, tmp_path, FakeHost())
    assert patches[0][0]["value"]["limits"] == cli.DEFAULT_LIMITS["credentialProxy"]
    assert patches[-1] == [{"op": "add", "path": "/spec/deployment/credentialProxy/resources",
                            "value": {"limits": {"memory": "8G"}}}]


def test_scenario_restores_and_reports_when_the_trigger_fails(monkeypatch, tmp_path):
    def boom(h, b, l):
        raise SystemExit("trigger failed")
    host, before = FakeHost(), len(REPORTS)
    with pytest.raises(SystemExit, match="trigger failed"):
        run_scenario(monkeypatch, tmp_path, host, start=boom)
    assert host.patches[-1][0]["value"] == {"limits": {"memory": "8G"}}
    assert len(REPORTS) == before + 1


def test_scenario_stops_and_restores_when_the_lease_is_lost(monkeypatch, tmp_path):
    host = FakeHost()
    with pytest.raises(SystemExit, match="lease"):
        run_scenario(monkeypatch, tmp_path, host, lost=True)
    assert host.patches[-1][0]["value"] == {"limits": {"memory": "8G"}}


def test_signals_unwind_as_system_exit():
    with pytest.raises(SystemExit, match="signal 15"):
        cli.stop_on_signal(15, None)


def test_fleet_delete_only_takes_subnets_of_the_harness_shape(monkeypatch, tmp_path):
    args = cli.parse(["--project", "p", "--prefix", "kwok-", "--out", str(tmp_path), "fleet", "delete"])
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:4] == ["gcloud", "compute", "networks", "subnets"] and cmd[4] == "list":
            return "kwok-01-subnet\nkwok-team-subnet\nkwok-03-subnet-old\nother-01-subnet\n"
        return ""
    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli.Fleet, "names", lambda self: [])
    cli.fleet_delete(args)
    deleted = [c[5] for c in calls if c[:5] == ["gcloud", "compute", "networks", "subnets", "delete"]]
    assert deleted == ["kwok-01-subnet"]
    listing = next(c for c in calls if "list" in c)
    assert "--network" in listing and "default" in listing
