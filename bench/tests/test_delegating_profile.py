"""Tests for delegating_profile: the throwaway delegating AgentProfile a case
runs against (#6789), its harness wiring, and the validator's rules for it."""

from __future__ import annotations

import copy
import datetime
import importlib.util
import json
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest
from kube_agents_bench import cases, delegating_profile, harness
from kube_agents_bench.delegating_profile import DelegatingProfileError, Profile

REPO = Path(__file__).resolve().parents[2]
NS = "kubeagents-system"
POOLS_NS = NS + "-profiles"
CLUSTER_SPEC: dict[str, Any] = {
    "description": "Single-cluster operations.",
    "persona": {"image": "example.test/persona-cluster:v1"},
    "harness": {"image": "example.test/worker:v1", "model": "m"},
    "identity": {},
    "yieldToPinned": True,
    "lifecycle": {"activeDeadlineSeconds": 1800},
    "concurrency": 4,
    "allowedTiers": ["read", "unattended"],
    "parameters": [{"name": "project", "required": True}, {"name": "cluster", "required": True}],
    "resources": {"requests": {"cpu": "1", "memory": "2Gi"}},
}
CLUSTER_PROFILE: dict[str, Any] = {
    "apiVersion": "kubeagents.x-k8s.io/v1alpha1",
    "kind": "AgentProfile",
    "metadata": {"name": "cluster", "namespace": NS, "uid": "u-1", "resourceVersion": "42", "labels": {"helm": "x"}},
    "spec": CLUSTER_SPEC,
    "status": {"conditions": [{"type": "CardPublished", "status": "True"}]},
}


def _crd(delegates: bool) -> dict[str, Any]:
    props: dict[str, Any] = {"description": {}, "yieldToPinned": {}}
    if delegates:
        props["delegates"] = {"type": "boolean"}
    return {"spec": {"versions": [{"name": "v1alpha1", "served": True, "schema": {"openAPIV3Schema": {"properties": {"spec": {"properties": props}}}}}]}}


def _done(stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["kubectl"], rc, stdout, stderr)


class FakeCluster:
    """kubectl against an install: profiles in NS, pool Deployments in POOLS_NS.

    The operator is modelled by counts: a created profile publishes its card on
    its `publish_after`th read, and a deleted one's pools stay for
    `pools_linger` reads of the pool namespace (forever when None)."""

    def __init__(self, *, delegates_in_crd: bool = True, publish_after: int | None = 1, pools_linger: int | None = 1):
        self.profiles: dict[str, dict[str, Any]] = {"cluster": copy.deepcopy(CLUSTER_PROFILE)}
        self.pools: dict[str, set[str]] = {}
        self.crd = _crd(delegates_in_crd)
        self.publish_after = publish_after
        self.pools_linger = pools_linger
        self.reads: dict[str, int] = {}
        self.linger: dict[str, int | None] = {}
        self.calls: list[tuple[str, ...]] = []
        self.stdins: list[str] = []
        self.fail_delete = 0
        self.fail_create = False
        #: The server stores the profile, then the create fails (a timeout)
        #: or is cut short (a signal), as a real create can.
        self.create_stored_then: str | None = None

    def __call__(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if stdin is not None:
            self.stdins.append(stdin)
        verb, rest = args[0], list(args[1:])
        if verb == "create":
            if self.fail_create:
                return _done(rc=1, stderr="admission webhook refused")
            obj = json.loads(stdin or "{}")
            name = obj["metadata"]["name"]
            if name in self.profiles:
                return _done(rc=1, stderr=f'Error from server (AlreadyExists): agentprofiles "{name}" already exists')
            self.profiles[name] = obj
            self.pools[name] = {f"agentprofile-{name}-{t}" for t in obj["spec"].get("allowedTiers") or ["read"]}
            if self.create_stored_then == "timeout":
                return _done(rc=1, stderr="context deadline exceeded")
            if self.create_stored_then == "interrupted":
                raise delegating_profile.Interrupted("signal 15")
            return _done(f"agentprofile.kubeagents.x-k8s.io/{name} created")
        if verb == "delete":
            if self.fail_delete:
                self.fail_delete -= 1
                return _done(rc=1, stderr="etcdserver: request timed out")
            name = rest[1]
            self.profiles.pop(name, None)
            self.linger.setdefault(name, self.pools_linger)
            return _done()
        assert verb == "get", args
        if rest[0] == "crd":
            return _done(json.dumps(self.crd))
        if rest[0] == "agentprofile":
            if "-l" in rest:
                items = [p for p in self.profiles.values() if (p["metadata"].get("labels") or {}).get(delegating_profile.LABEL)]
                return _done(json.dumps({"items": items}))
            name = rest[1]
            obj = self.profiles.get(name)
            if "--ignore-not-found" in rest:
                if "json" in rest:
                    return _done(json.dumps(obj) if obj else "")
                return _done(f"agentprofile.kubeagents.x-k8s.io/{name}\n" if obj else "")
            if obj is None:
                return _done(rc=1, stderr=f'agentprofiles "{name}" not found')
            self.reads[name] = self.reads.get(name, 0) + 1
            if name != "cluster" and self.publish_after is not None and self.reads[name] >= self.publish_after:
                obj = {**obj, "status": {"conditions": [{"type": "CardPublished", "status": "True"}]}}
            return _done(json.dumps(obj))
        if rest[0] in ("deployments", "deployment"):
            assert rest[rest.index("-n") + 1] == POOLS_NS
            self._age_pools()
            live = {n for pools in self.pools.values() for n in pools}
            if rest[0] == "deployments":
                profile = rest[rest.index("-l") + 1].split("=", 1)[1]
                found = sorted(self.pools.get(profile, set()))
            else:
                found = [rest[1]] if rest[1] in live else []
            return _done("".join(f"deployment.apps/{n}\n" for n in found))
        raise AssertionError(f"unexpected kubectl {args}")

    def _age_pools(self) -> None:
        for name, left in list(self.linger.items()):
            if left is None:
                continue
            if left <= 0:
                self.pools.pop(name, None)
            else:
                self.linger[name] = left - 1


def _quiet(_m: str) -> None:
    pass


def _ticks() -> tuple[list[float], Any]:
    slept: list[float] = []
    return slept, (lambda: sum(slept))


# -- the manifest ----------------------------------------------------------------


def test_the_manifest_copies_the_cluster_spec_with_delegates_on_and_the_pin_yield_off() -> None:
    source = copy.deepcopy(CLUSTER_PROFILE)
    manifest = delegating_profile.build_manifest(source, "delegating-0a1b2c3d")
    spec = manifest["spec"]
    assert spec["delegates"] is True
    assert spec["yieldToPinned"] is False
    assert spec["description"] == delegating_profile.DESCRIPTION != CLUSTER_SPEC["description"]
    assert spec["identity"] == {}
    rest = {k: v for k, v in spec.items() if k not in ("delegates", "yieldToPinned", "description", "identity")}
    assert rest == {k: v for k, v in CLUSTER_SPEC.items() if k not in ("yieldToPinned", "description", "identity")}
    assert manifest["metadata"] == {
        "name": "delegating-0a1b2c3d",
        "namespace": NS,
        "labels": {delegating_profile.LABEL: delegating_profile.LABEL_VALUE},
    }
    assert "status" not in manifest
    assert source == CLUSTER_PROFILE, "the source is not mutated"


def test_the_manifest_drops_a_named_service_account() -> None:
    source = copy.deepcopy(CLUSTER_PROFILE)
    source["spec"]["identity"] = {"serviceAccountName": "agentprofile-cluster"}
    assert delegating_profile.build_manifest(source, "delegating-1")["spec"]["identity"] == {}


def test_without_the_field_in_the_api_the_manifest_leaves_delegates_out() -> None:
    source = copy.deepcopy(CLUSTER_PROFILE)
    source["spec"]["delegates"] = False
    spec = delegating_profile.build_manifest(source, "delegating-1", delegates=False)["spec"]
    assert "delegates" not in spec
    assert spec["yieldToPinned"] is False


def test_each_run_gets_a_unique_dns_1123_name() -> None:
    names = {delegating_profile.new_name() for _ in range(50)}
    assert len(names) == 50
    for name in names:
        assert delegating_profile.is_throwaway_name(name)
        assert len(name) < 56, "the operator refuses a profile name of 56 characters"
        assert delegating_profile.NAME_PATTERN.match(name)
    with pytest.raises(DelegatingProfileError):
        delegating_profile.new_name("Not_A_Name")


def test_the_crd_schema_says_whether_delegates_exists() -> None:
    assert delegating_profile.crd_declares_delegates(_crd(True))
    assert not delegating_profile.crd_declares_delegates(_crd(False))
    unserved = _crd(True)
    unserved["spec"]["versions"][0]["served"] = False
    assert not delegating_profile.crd_declares_delegates(unserved)


# -- setup ----------------------------------------------------------------------


def test_setup_creates_the_copy_on_stdin_and_waits_for_its_card() -> None:
    cluster = FakeCluster(publish_after=3)
    slept, clock = _ticks()
    profile = delegating_profile.setup(cluster, NS, token="0a1b2c3d", sleep=slept.append, clock=clock, log=_quiet)
    assert profile == Profile(name="delegating-0a1b2c3d", namespace=NS, tiers=("read", "unattended"), delegates=True)
    assert ("get", "agentprofile", "cluster", "-n", NS, "-o", "json") in cluster.calls
    assert ("create", "-n", NS, "-f", "-") in cluster.calls
    created = json.loads(cluster.stdins[0])
    assert created["spec"]["delegates"] is True and created["spec"]["yieldToPinned"] is False
    assert len(slept) == 2, "two reads before the card published"
    # Nothing touches a Secret, and the manifest goes on stdin, not to a file.
    assert not any("secret" in a.lower() for call in cluster.calls for a in call)
    assert all(call[call.index("-f") + 1] == "-" for call in cluster.calls if "-f" in call)


def test_setup_on_an_api_without_delegates_creates_the_copy_without_it() -> None:
    cluster = FakeCluster(delegates_in_crd=False)
    logged: list[str] = []
    profile = delegating_profile.setup(cluster, NS, token="1", sleep=lambda _s: None, log=logged.append)
    assert profile.delegates is False
    assert "delegates" not in json.loads(cluster.stdins[0])["spec"]
    assert any("declares no spec.delegates" in m for m in logged)


def test_a_card_that_never_publishes_fails_setup_and_tears_the_profile_down() -> None:
    cluster = FakeCluster(publish_after=None)
    slept, clock = _ticks()
    with pytest.raises(DelegatingProfileError, match="CardPublished is not True"):
        delegating_profile.setup(cluster, NS, token="2", sleep=slept.append, clock=clock, log=_quiet)
    assert "delegating-2" not in cluster.profiles
    assert any(c[0] == "delete" for c in cluster.calls)


def test_a_refused_create_fails_setup_with_kubectls_reason() -> None:
    cluster = FakeCluster()
    cluster.fail_create = True
    with pytest.raises(DelegatingProfileError, match="admission webhook refused"):
        delegating_profile.setup(cluster, NS, token="3", sleep=lambda _s: None, log=_quiet)


# -- teardown -------------------------------------------------------------------


def _made(cluster: FakeCluster, token: str = "4") -> Profile:
    return delegating_profile.setup(cluster, NS, token=token, sleep=lambda _s: None, log=_quiet)


def test_teardown_waits_until_the_profile_and_every_pool_are_gone() -> None:
    cluster = FakeCluster(pools_linger=2)
    profile = _made(cluster)
    assert cluster.pools[profile.name] == {f"agentprofile-{profile.name}-read", f"agentprofile-{profile.name}-unattended"}
    slept, clock = _ticks()
    delegating_profile.teardown(cluster, profile, sleep=slept.append, clock=clock, log=_quiet)
    assert profile.name not in cluster.profiles
    assert profile.name not in cluster.pools
    assert slept, "it waited for the pools rather than trusting the delete"
    pool_reads = [c for c in cluster.calls if c[:2] == ("get", "deployment")]
    assert {c[2] for c in pool_reads} == {f"agentprofile-{profile.name}-read", f"agentprofile-{profile.name}-unattended"}
    assert ("delete", "agentprofile", profile.name, "-n", NS, "--ignore-not-found", "--wait=false") in cluster.calls


def test_a_pool_that_never_goes_is_an_error_naming_it() -> None:
    cluster = FakeCluster(pools_linger=None)
    profile = _made(cluster)
    slept, clock = _ticks()
    with pytest.raises(DelegatingProfileError, match=f"deployment {POOLS_NS}/agentprofile-{profile.name}-read"):
        delegating_profile.teardown(cluster, profile, sleep=slept.append, clock=clock, log=_quiet)
    assert sum(slept) >= delegating_profile.TEARDOWN_SECONDS


def test_a_failed_delete_is_retried_inside_the_bound() -> None:
    cluster = FakeCluster()
    profile = _made(cluster)
    cluster.fail_delete = 2
    delegating_profile.teardown(cluster, profile, sleep=lambda _s: None, log=_quiet)
    assert [c[0] for c in cluster.calls].count("delete") == 3
    assert profile.name not in cluster.profiles


def test_a_delete_that_never_succeeds_is_an_error() -> None:
    cluster = FakeCluster()
    profile = _made(cluster)
    cluster.fail_delete = 10_000
    slept, clock = _ticks()
    with pytest.raises(DelegatingProfileError, match="request timed out"):
        delegating_profile.teardown(cluster, profile, sleep=slept.append, clock=clock, log=_quiet)


def test_sweep_tears_down_only_old_labelled_throwaways() -> None:
    cluster = FakeCluster(pools_linger=0)
    old, young = _made(cluster, "aaaa"), _made(cluster, "bbbb")
    now = datetime.datetime(2026, 11, 24, 12, tzinfo=datetime.timezone.utc)
    cluster.profiles[old.name]["metadata"]["creationTimestamp"] = "2026-11-24T01:00:00Z"
    cluster.profiles[young.name]["metadata"]["creationTimestamp"] = "2026-11-24T11:30:00Z"
    swept = delegating_profile.sweep(cluster, NS, now=now, sleep=lambda _s: None, log=_quiet)
    assert swept == [old.name]
    assert young.name in cluster.profiles and "cluster" in cluster.profiles


def test_sigterm_raises_inside_the_window_and_the_handler_is_put_back() -> None:
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(delegating_profile.Interrupted), delegating_profile.signals_raise():
        signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == before


def test_the_key_is_a_case_key() -> None:
    assert delegating_profile.KEY in cases.CASE_KEYS


# -- the harness ----------------------------------------------------------------

PROMPT = (
    f"[bench:profile name={delegating_profile.PLACEHOLDER} project=p1 location=l1 cluster=c1] "
    "ask the cluster profile what is failing"
)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    """The harness on the inject transport, its kubectl the fake, no waits."""
    cluster = FakeCluster()
    monkeypatch.setenv("AGENT_TRANSPORT", harness.TRANSPORT_INJECT)
    monkeypatch.setenv("AGENT_NAMESPACE", NS)
    monkeypatch.setattr(delegating_profile, "kubectl_runner", lambda _context=None: cluster)
    monkeypatch.setattr(delegating_profile.time, "sleep", lambda _s: None)
    monkeypatch.setattr(harness, "_pod_start", lambda: (None, False))
    return cluster


def _execute_with(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> list[str]:
    seen: list[str] = []

    def fake_execute(self: Any, prompt: str, workspace_path: Any = None, **_kw: Any) -> harness.AgentResult:
        seen.append(prompt)
        return behaviour(prompt)

    monkeypatch.setattr(harness.KubeAgentsHarness, "_execute", fake_execute)
    return seen


def _deleted(cluster: FakeCluster) -> list[str]:
    return [c[2] for c in cluster.calls if c[0] == "delete"]


def test_the_ask_addresses_the_profile_and_teardown_follows_a_graded_run(
    wired: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _execute_with(monkeypatch, lambda _p: harness.AgentResult(output="ok", trajectory=[]))
    result = harness.KubeAgentsHarness().run(PROMPT)
    assert not result.has_errors()
    [sent] = seen
    name = sent.split("name=", 1)[1].split()[0]
    assert delegating_profile.is_throwaway_name(name)
    assert delegating_profile.PLACEHOLDER not in sent
    assert _deleted(wired) == [name]
    assert name not in wired.profiles and name not in wired.pools


def test_teardown_runs_when_the_case_fails(wired: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    _execute_with(monkeypatch, lambda _p: harness.AgentResult.errored("the agent gave up"))
    result = harness.KubeAgentsHarness().run(PROMPT)
    assert result.has_errors()
    assert len(_deleted(wired)) == 1
    assert set(wired.profiles) == {"cluster"}


def test_teardown_runs_when_the_run_raises(wired: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_p: str) -> harness.AgentResult:
        raise RuntimeError("transport fell over")

    _execute_with(monkeypatch, boom)
    result = harness.KubeAgentsHarness().run(PROMPT)
    assert result.has_errors()
    assert len(_deleted(wired)) == 1


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, delegating_profile.Interrupted])
def test_teardown_runs_when_the_run_is_interrupted(
    wired: FakeCluster, monkeypatch: pytest.MonkeyPatch, interrupt: type[BaseException]
) -> None:
    def stop(_p: str) -> harness.AgentResult:
        raise interrupt()

    _execute_with(monkeypatch, stop)
    with pytest.raises(interrupt):
        harness.KubeAgentsHarness().run(PROMPT)
    assert len(_deleted(wired)) == 1
    assert set(wired.profiles) == {"cluster"}


def test_a_teardown_it_cannot_confirm_makes_the_run_infrastructure(
    wired: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired.pools_linger = None
    monkeypatch.setattr(delegating_profile, "TEARDOWN_SECONDS", 0)
    _execute_with(monkeypatch, lambda _p: harness.AgentResult(output="a fine answer", trajectory=[]))
    result = harness.KubeAgentsHarness().run(PROMPT)
    assert result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "not torn down" in result.errors[0]


def test_a_failed_setup_is_infrastructure_and_sends_nothing(
    wired: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired.fail_create = True
    seen = _execute_with(monkeypatch, lambda _p: harness.AgentResult(output="", trajectory=[]))
    result = harness.KubeAgentsHarness().run(PROMPT)
    assert result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert "could not be set up" in result.errors[0]
    assert seen == []


@pytest.mark.parametrize("transport", [harness.TRANSPORT_API, harness.TRANSPORT_SLACK])
def test_another_transport_is_refused_before_anything_is_made(
    wired: FakeCluster, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    monkeypatch.setenv("AGENT_TRANSPORT", transport)
    seen = _execute_with(monkeypatch, lambda _p: harness.AgentResult(output="", trajectory=[]))
    result = harness.KubeAgentsHarness().run(PROMPT)
    assert result.has_errors()
    assert f"needs AGENT_TRANSPORT={harness.TRANSPORT_INJECT}" in result.errors[0]
    assert wired.calls == [] and seen == []


def test_a_prompt_without_the_placeholder_makes_no_profile(wired: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    _execute_with(monkeypatch, lambda _p: harness.AgentResult(output="ok", trajectory=[]))
    harness.KubeAgentsHarness().run("[bench:profile name=cluster project=p location=l cluster=c] ask")
    assert wired.calls == []


# -- the validator --------------------------------------------------------------


@pytest.fixture(scope="module")
def validator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "validate_bench_cases_for_delegating_profile_tests", REPO / "scripts" / "validate_bench_cases.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _problems(validator: Any, spec: dict[str, Any], name: str = "made-up-case") -> list[str]:
    problems: list[str] = []
    validator._profile_directive(spec, problems)
    validator._delegating_profile(name, spec, problems)
    return problems


def test_a_declared_and_addressed_case_passes(validator: Any) -> None:
    assert _problems(validator, {"prompt": PROMPT, "delegating_profile": True}) == []


def test_addressing_the_profile_without_declaring_it_is_refused(validator: Any) -> None:
    [problem] = _problems(validator, {"prompt": PROMPT})
    assert "without 'delegating_profile: true'" in problem


def test_declaring_it_without_addressing_it_is_refused(validator: Any) -> None:
    for prompt in (
        "[bench:profile name=cluster project=p location=l cluster=c] ask",
        f"ask about {delegating_profile.PLACEHOLDER}",
        f"[bench:profile name={delegating_profile.PLACEHOLDER}] ask {delegating_profile.PLACEHOLDER} again",
    ):
        problems = _problems(validator, {"prompt": prompt, "delegating_profile": True})
        assert any("needs the prompt to open with [bench:profile name=" in p for p in problems), prompt


def test_a_literal_throwaway_name_is_refused(validator: Any) -> None:
    problems = _problems(validator, {"prompt": "[bench:profile name=delegating-0a1b2c3d] ask"})
    assert any("names a throwaway delegating profile literally" in p for p in problems)


@pytest.mark.parametrize("value", ["true", "yes", 1, False, {"from": "cluster"}])
def test_the_key_must_be_a_bare_true(validator: Any, value: Any) -> None:
    problems = _problems(validator, {"prompt": PROMPT, "delegating_profile": value})
    assert any("is not a bare true" in p for p in problems)


def test_the_placeholder_outside_the_prompt_is_refused(validator: Any) -> None:
    spec = {
        "prompt": PROMPT,
        "delegating_profile": True,
        "verification_spec": [{"check": {"type": "report_contains", "patterns": [delegating_profile.PLACEHOLDER]}}],
    }
    problems = _problems(validator, spec)
    assert any(p.startswith("verification_spec[0].check.patterns[0]: carries") for p in problems)


def test_a_malformed_placeholder_is_refused(validator: Any) -> None:
    problems = _problems(validator, {"prompt": "[bench:profile name={{ BENCH_DELEGATING_PROFILE }}] ask"})
    assert any("malformed" in p for p in problems)


def test_a_case_on_the_slack_lane_is_refused(validator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lane = tmp_path / "cases.txt"
    lane.write_text("# comment\nother-case\nmade-up-case then: thanks\n", encoding="utf-8")
    monkeypatch.setattr(validator, "SLACK_LANE_CASES_FILE", lane)
    monkeypatch.setattr(validator, "REPO_ROOT", tmp_path)
    problems = _problems(validator, {"prompt": PROMPT, "delegating_profile": True})
    assert any("on the Slack lane" in p for p in problems)
    assert _problems(validator, {"prompt": PROMPT, "delegating_profile": True}, name="another-case") == []


# -- review fixes -----------------------------------------------------------------


@pytest.mark.parametrize("how", ["timeout", "interrupted"])
def test_a_create_that_fails_after_the_server_stored_it_is_torn_down(how: str) -> None:
    cluster = FakeCluster(pools_linger=0)
    cluster.create_stored_then = how
    raised = DelegatingProfileError if how == "timeout" else delegating_profile.Interrupted
    with pytest.raises(raised):
        delegating_profile.setup(cluster, NS, token="c0", sleep=lambda _s: None, log=_quiet)
    assert set(cluster.profiles) == {"cluster"}, "the profile the server stored was left behind"
    assert any(c[0] == "delete" and "--ignore-not-found" in c for c in cluster.calls)


def test_sweep_tries_every_leftover_and_reports_the_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = FakeCluster(pools_linger=0)
    first, second = _made(cluster, "aaa1"), _made(cluster, "bbb2")
    for p in (first, second):
        cluster.profiles[p.name]["metadata"]["creationTimestamp"] = "2026-11-24T01:00:00Z"
    real = delegating_profile.teardown
    tried: list[str] = []

    def teardown(kubectl: Any, profile: Profile, **kw: Any) -> None:
        tried.append(profile.name)
        if profile.name == first.name:
            raise DelegatingProfileError(f"{profile.name} stuck")
        real(kubectl, profile, **kw)

    monkeypatch.setattr(delegating_profile, "teardown", teardown)
    now = datetime.datetime(2026, 11, 24, 12, tzinfo=datetime.timezone.utc)
    with pytest.raises(DelegatingProfileError, match=f"{first.name} stuck"):
        delegating_profile.sweep(cluster, NS, now=now, sleep=lambda _s: None, log=_quiet)
    assert sorted(tried) == sorted([first.name, second.name])
    assert second.name not in cluster.profiles


def test_teardown_installs_no_signal_handler() -> None:
    # A second stop during teardown is not held (Brian, #7191, B): it can cut
    # the teardown short, and the leftover is the sweep's.
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    seen: list[Any] = []
    cluster = FakeCluster(pools_linger=0)
    made = _made(cluster, "fade")
    real = cluster.__call__

    def kubectl(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        seen.append(signal.getsignal(signal.SIGTERM))
        return real(*args, stdin=stdin)

    delegating_profile.teardown(kubectl, made, sleep=lambda _s: None, log=_quiet)
    assert all(h == before[signal.SIGTERM] for h in seen), "teardown changed the SIGTERM handler"
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_a_name_another_run_holds_is_not_torn_down() -> None:
    cluster = FakeCluster(pools_linger=0)
    other = _made(cluster, "beef")
    with pytest.raises(delegating_profile.NameTaken):
        delegating_profile.setup(cluster, NS, token="beef", sleep=lambda _s: None, log=_quiet)
    assert other.name in cluster.profiles, "another run's profile was deleted"


def test_the_teardown_command_reports_unreadable_output() -> None:
    def kubectl(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return _done("not json")

    assert delegating_profile.main(["teardown", "--name", "delegating-abc", "--namespace", NS], kubectl=kubectl) == 1


def test_a_throwaway_name_is_the_prefix_then_hex_only() -> None:
    assert delegating_profile.is_throwaway_name(delegating_profile.new_name())
    assert delegating_profile.is_throwaway_name("delegating-3fa2c9d1")
    for name in ("delegating-prod", "delegating-", "delegating-3FA2", "cluster"):
        assert not delegating_profile.is_throwaway_name(name), name


def test_the_teardown_command_refuses_a_name_or_profile_the_harness_did_not_make() -> None:
    cluster = FakeCluster(pools_linger=0)
    assert delegating_profile.main(["teardown", "--name", "delegating-prod", "--namespace", NS], kubectl=cluster) == 1
    hand = copy.deepcopy(CLUSTER_PROFILE)
    hand["metadata"] = {"name": "delegating-abc123", "namespace": NS}
    cluster.profiles["delegating-abc123"] = hand
    assert delegating_profile.main(["teardown", "--name", "delegating-abc123", "--namespace", NS], kubectl=cluster) == 1
    assert "delegating-abc123" in cluster.profiles, "an unlabelled profile was deleted"
    made = _made(cluster, "abcd")
    assert delegating_profile.main(["teardown", "--name", made.name, "--namespace", NS], kubectl=cluster) == 0
    assert made.name not in cluster.profiles


def test_a_literal_prefixed_name_is_still_refused_by_the_validator(validator: Any) -> None:
    # Broader than a name the harness makes: no case should name one at all.
    problems = _problems(validator, {"prompt": "[bench:profile name=delegating-prod] ask"})
    assert any("names a throwaway delegating profile literally" in p for p in problems)


def test_a_literal_sample_name_beside_a_placeholder_elsewhere_is_refused(validator: Any) -> None:
    sample = delegating_profile.new_name("0a1b2c3d")
    prompt = f"[bench:profile name={sample}]\nDelegate from {delegating_profile.PLACEHOLDER}."
    problems = _problems(validator, {"prompt": prompt, "delegating_profile": True})
    assert any("needs the prompt to open with" in p for p in problems), problems


def test_a_slack_lane_entry_with_an_inline_comment_is_still_caught(
    validator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "cases.txt"
    lane.write_text("made-up-case#note\n", encoding="utf-8")
    monkeypatch.setattr(validator, "SLACK_LANE_CASES_FILE", lane)
    monkeypatch.setattr(validator, "REPO_ROOT", tmp_path)
    problems = _problems(validator, {"prompt": PROMPT, "delegating_profile": True})
    assert any("on the Slack lane" in p for p in problems)
