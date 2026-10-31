# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for cluster-agent-fleet-health-triage's verification checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry

from kube_agents_bench import transcript

CASE_ID = "cluster-agent-fleet-health-triage"
REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = REPO_ROOT / "bench" / "tasks" / CASE_ID / "task.yaml"

SCAN_RAN = "the-scan-ran-once-per-cluster-question"
NO_POLL_OR_EXPLORE = "the-scan-did-not-poll-or-explore"
NO_RECHECK = "the-clean-scan-was-not-re-checked"
RCA_OOM = "rca-names-the-oom"

SEEDED_A = "cluster-gca-gke-test-gga-seeded-a-us-central1-c"
SEEDED_B = "cluster-gca-gke-test-gga-seeded-b-us-central1-b"
SEEDED_C = "cluster-gca-gke-test-gga-seeded-c-us-central1-c"
SEEDED_D = "cluster-gca-gke-test-gga-seeded-d-us-central1-f"

PREFLIGHT_CMD = "/opt/data/scripts/cluster_preflight.sh --json"
SCAN_CMD = "python3 /opt/data/scripts/workload_triage.py"
SCAN_JSON_CMD = "python3 /opt/data/scripts/workload_triage.py --json"

ANSWER = (
    "Healthy clusters: no failing workloads. On seeded-a, payments-api in "
    "seeded-debug is crashlooping because its api container is OOMKilled against "
    "its 64Mi memory limit."
)


def _entry(name: str) -> VerificationEntry:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (raw,) = [e for e in doc["verification_spec"] if e.get("name") == name]
    return VerificationEntry.model_validate(raw)


def _worker_call(
    card: str,
    at: float,
    command: str,
    result: str = "",
    agent: str = SEEDED_B,
) -> dict[str, Any]:
    return {
        "name": "terminal",
        "agent": agent,
        "task": card,
        "at": at,
        "args": {"command": command},
        "result": result,
        "status": "completed",
    }


def _status(
    name: str,
    calls: list[dict[str, Any]],
    output: str = ANSWER,
    parents: dict[str, str] | None = None,
    worker_commands: list[dict[str, str]] | None = None,
) -> str:
    if worker_commands is None:
        worker_commands = [
            {"task": c["task"], "command": c["args"]["command"]}
            for c in calls
            if c.get("name") == "terminal" and "command" in c.get("args", {})
        ]
    transcript.set(
        output,
        calls,
        final_message=output,
        worker_commands=worker_commands,
        worker_capture_gaps=[],
        worker_card_parents=parents or {},
    )
    try:
        res = VerifierAgent().run_entry(_entry(name), timeout_sec=5.0)
        return res.status
    finally:
        transcript.clear()


def test_green_fleet_run_passes_all_transcript_checks():
    calls = [
        # Platform coordinator polling its child cards with sleep 60 must not
        # trip healthy-cluster worker checks.
        _worker_call("card-platform", 5.0, "sleep 60", "", agent="platform"),
        # Healthy clusters (seeded-b and seeded-c) run preflight + scan and exit.
        _worker_call("card-b", 10.0, PREFLIGHT_CMD, "{}", agent=SEEDED_B),
        _worker_call("card-b", 11.0, SCAN_CMD, "unhealthy: 0\nsystem unhealthy: 0", agent=SEEDED_B),
        _worker_call("card-c", 10.5, PREFLIGHT_CMD, "{}", agent=SEEDED_C),
        _worker_call("card-c", 11.5, SCAN_JSON_CMD, '{"summary": {"unhealthy": 0, "system_unhealthy": 0}}', agent=SEEDED_C),
        # Unhealthy OOM cluster (seeded-a) and slot d (seeded-d reading
        # cluster-autoscaler-status in kube-system, as in run 165316, or a slow
        # model turn gap on seeded-a, as in run 131852) must not trip the
        # healthy-cluster checks.
        _worker_call("card-oom", 12.0, SCAN_CMD, "unhealthy: 1\nsystem unhealthy: 0", agent=SEEDED_A),
        _worker_call(
            "card-oom",
            160.0,
            "kubectl get pod payments-api-7f8 -n seeded-debug -o json",
            "{}",
            agent=SEEDED_A,
        ),
        _worker_call(
            "card-oom",
            162.0,
            "kubectl logs payments-api-7f8 -n seeded-debug -c api --tail=50 -p",
            "OOM",
            agent=SEEDED_A,
        ),
        _worker_call(
            "card-d",
            20.0,
            "kubectl describe configmap cluster-autoscaler-status -n kube-system",
            "Status: Healthy",
            agent=SEEDED_D,
        ),
    ]
    for check in (SCAN_RAN, NO_POLL_OR_EXPLORE, NO_RECHECK, RCA_OOM):
        assert _status(check, calls) == "pass", check


def test_coordinator_sweep_with_single_flagged_cluster_card_passes():
    # Reproduces run 171130 (r2): when the coordinator answers seeded-b and
    # seeded-c from fleet_health.py and cards only seeded-a, which runs
    # workload_triage.py, all transcript checks still pass.
    calls = [
        _worker_call(
            "card-platform",
            5.0,
            "python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/fleet_health.py --output /opt/data/scratch/chat.json",
            "wrote the manifest",
            agent="platform",
        ),
        _worker_call("card-oom", 12.0, SCAN_CMD, "unhealthy: 1\nsystem unhealthy: 0", agent=SEEDED_A),
        _worker_call(
            "card-oom",
            14.0,
            "kubectl get pod payments-api-7f8 -n seeded-debug -o json",
            "{}",
            agent=SEEDED_A,
        ),
    ]
    for check in (SCAN_RAN, NO_POLL_OR_EXPLORE, NO_RECHECK, RCA_OOM):
        assert _status(check, calls) == "pass", check


def test_missing_workload_triage_fails_scan_ran_check():
    # Reproduces the upstream/main red run (142843): no worker runs workload_triage.py.
    calls = [
        _worker_call("card-b", 10.0, "kubectl get pods -A", "NAME READY", agent=SEEDED_B),
        _worker_call("card-c", 10.5, "kubectl get events -n kube-system", "LAST SEEN", agent=SEEDED_C),
    ]
    assert _status(SCAN_RAN, calls) == "fail"
    assert _status(NO_POLL_OR_EXPLORE, calls) == "fail"
    assert _status(NO_RECHECK, calls) == "fail"


@pytest.mark.parametrize("healthy_agent", [SEEDED_B, SEEDED_C])
@pytest.mark.parametrize(
    "bad_command",
    [
        "sleep 15",
        "watch -n 2 kubectl get pods -n seeded-debug",
        "kubectl get pods -n seeded-debug -w",
        "kubectl get pods -n seeded-debug --watch",
        "kubectl get pods -n seeded-debug --watch-only",
        "kubectl logs payments-api-7f8 -n seeded-debug -f",
        "kubectl logs payments-api-7f8 -n seeded-debug --follow",
        "kubectl attach payments-api-7f8 -n seeded-debug",
        "kubectl wait --for=condition=Ready pod/payments-api-7f8 -n seeded-debug",
        "kubectl rollout status deploy/payments-api -n seeded-debug",
        "kubectl port-forward pod/payments-api-7f8 8080:8080 -n seeded-debug",
        "kubectl proxy",
        "gcloud container clusters get-credentials seeded-b --region us-central1",
        "gcloud container fleet memberships get-credentials my-membership",
        "kubectl get pods -n kube-system",
        "kubectl get pods -nkube-system",
        "kubectl get pods -n=kube-system",
        "kubectl get events -n gke-managed-cim",
        "kubectl -n gmp-system get pods",
        "kubectl --namespace=cnrm-system describe pod cnrm-controller-manager-0",
        "kubectl logs istiod-abc -n istio-system",
        "kubectl get ns kube-system -o yaml",
        "kubectl describe ns gke-managed-cim",
        "for ns in kube-system gke-managed-cim; do kubectl get pods -n $ns; done",
        'NS=kube-system; kubectl get pods -n "$NS"',
        "until kubectl get pod payments-api-7f8 -n seeded-debug -o jsonpath='{.status.phase}' | grep -q Running; do :; done",
        "timeout 60 tail -f /dev/null",
        "ping -c 15 127.0.0.1",
        "read -t 60 _",
        "perl -e 'select(undef,undef,undef,60)'",
        "python3 /opt/data/scripts/workload_triage.py --include-system",
    ],
)
def test_poll_or_system_exploration_on_healthy_cluster_fails(
    healthy_agent: str, bad_command: str
):
    calls = [
        _worker_call("card-h", 10.0, SCAN_CMD, "unhealthy: 0\nsystem unhealthy: 0", agent=healthy_agent),
        _worker_call("card-h", 11.0, bad_command, "", agent=healthy_agent),
    ]
    assert _status(NO_POLL_OR_EXPLORE, calls) == "fail", (healthy_agent, bad_command)


@pytest.mark.parametrize("healthy_agent", [SEEDED_B, SEEDED_C])
def test_execute_code_mcp_or_command_budget_exceeded_on_healthy_cluster_fails(healthy_agent: str):
    for code_body in (
        "import time\ntime.sleep(15)\n",
        'import subprocess\nsubprocess.run(["kubectl", "get", "pods", "-n", "kube-system"])\n',
        'import subprocess\nsubprocess.run(["gcloud", "container", "clusters", "get-credentials", "x"])\n',
        "import select\nselect.select([], [], [], 60)\n",
    ):
        calls = [
            _worker_call("card-h", 10.0, SCAN_CMD, "unhealthy: 0\nsystem unhealthy: 0", agent=healthy_agent),
            {
                "name": "execute_code",
                "agent": healthy_agent,
                "task": "card-h",
                "at": 11.0,
                "args": {"code": code_body},
                "result": "",
                "status": "completed",
            },
        ]
        assert _status(NO_POLL_OR_EXPLORE, calls) == "fail", code_body

    for mcp_call in (
        {
            "name": "mcp__gke__list_k8s_events",
            "agent": healthy_agent,
            "task": "card-h",
            "at": 11.0,
            "args": {"namespace": "kube-system"},
            "result": "{}",
            "status": "completed",
        },
        {
            "name": "tool_call",
            "agent": healthy_agent,
            "task": "card-h",
            "at": 11.0,
            "args": {
                "name": "mcp__gke__get_k8s_rollout_status",
                "arguments": {"name": "payments-api", "namespace": "seeded-debug"},
            },
            "result": "{}",
            "status": "completed",
        },
    ):
        calls = [
            _worker_call("card-h", 10.0, SCAN_CMD, "unhealthy: 0\nsystem unhealthy: 0", agent=healthy_agent),
            mcp_call,
        ]
        assert _status(NO_POLL_OR_EXPLORE, calls) == "fail", mcp_call

    # 6 calls on a healthy cluster pass; 7 calls exceed the 6-command scan budget and fail.
    six_calls = [
        _worker_call("card-h", 10.0 + i, PREFLIGHT_CMD, "{}", agent=healthy_agent)
        for i in range(6)
    ]
    assert _status(NO_POLL_OR_EXPLORE, six_calls) == "pass"
    seven_calls = [
        *six_calls,
        _worker_call("card-h", 20.0, PREFLIGHT_CMD, "{}", agent=healthy_agent),
    ]
    assert _status(NO_POLL_OR_EXPLORE, seven_calls) == "fail"


@pytest.mark.parametrize("healthy_agent", [SEEDED_B, SEEDED_C])
@pytest.mark.parametrize(
    "recheck_command",
    [
        "python3 /opt/data/scripts/workload_triage.py",
        "python3 /opt/data/scripts/workload_triage.py --json",
        "python3 /opt/data/scripts/workload_triage.py --events-minutes 1440",
        "cd /opt/data/scripts && python3 -m workload_triage",
        "sudo python3 /opt/data/scripts/workload_triage.py",
        "/usr/bin/python3 /opt/data/scripts/workload_triage.py",
        "PYTHONPATH=/opt/data/scripts python3 -m workload_triage",
        "bash -c 'python3 /opt/data/scripts/workload_triage.py'",
        "exec python3 /opt/data/scripts/workload_triage.py",
        "time python3 /opt/data/scripts/workload_triage.py",
        "timeout 30 python3 /opt/data/scripts/workload_triage.py",
        "kubectl get pods -A",
        "kubectl get -A pods",
        "kubectl get sts -A",
        "kubectl get statefulsets --all-namespaces",
        "kubectl get jobs -A",
        "kubectl get pods -Ao wide",
        "kubectl events -A",
        "kubectl top pods --all-namespaces",
        "kubectl describe pods -A",
        "kubectl get --raw /api/v1/pods",
        "kubectl get --raw=/api/v1/events",
        "kubectl get --raw /apis/apps/v1/deployments",
        "kubectl get --raw /apis/apps/v1/replicasets",
        "kubectl get --raw /apis/metrics.k8s.io/v1beta1/pods",
        "kubectl get --raw /apis/batch/v1/jobs",
        "timeout -k 5 30 python3 /opt/data/scripts/workload_triage.py",
        "timeout --kill-after 5 --signal TERM 30 python3 /opt/data/scripts/workload_triage.py",
        "nice -n 10 python3 /opt/data/scripts/workload_triage.py",
        "command -p python3 /opt/data/scripts/workload_triage.py",
    ],
)
def test_rechecking_or_all_namespaces_on_healthy_cluster_fails(
    healthy_agent: str, recheck_command: str
):
    calls = [
        _worker_call("card-h", 10.0, SCAN_CMD, "unhealthy: 0\nsystem unhealthy: 0", agent=healthy_agent),
        _worker_call("card-h", 11.0, recheck_command, "", agent=healthy_agent),
    ]
    assert _status(NO_RECHECK, calls) == "fail", (healthy_agent, recheck_command)


@pytest.mark.parametrize("sep", [" && ", "; ", "\n", " & ", " || "])
def test_two_scans_in_one_call_or_across_retried_cards_on_healthy_cluster_fails(sep: str):
    chained = [
        _worker_call(
            "card-b",
            10.0,
            f"{SCAN_CMD}{sep}{SCAN_JSON_CMD}",
            "unhealthy: 0\nsystem unhealthy: 0",
            agent=SEEDED_B,
        ),
    ]
    assert _status(NO_RECHECK, chained) == "fail", sep

    retried = [
        _worker_call("card-b-1", 10.0, SCAN_CMD, "unhealthy: 0\nsystem unhealthy: 0", agent=SEEDED_B),
        _worker_call("card-b-2", 20.0, SCAN_CMD, "unhealthy: 0\nsystem unhealthy: 0", agent=SEEDED_B),
    ]
    assert _status(NO_RECHECK, retried) == "fail"


def test_help_flag_and_non_watch_or_single_namespace_flags_do_not_false_red():
    calls = [
        _worker_call("card-b", 10.0, "python3 /opt/data/scripts/workload_triage.py --help", "usage:", agent=SEEDED_B),
        _worker_call("card-b", 10.5, "cat /opt/data/scripts/workload_triage.py", "#!/usr/bin/env python3", agent=SEEDED_B),
        _worker_call(
            "card-b",
            11.0,
            'OUT=/tmp/workload_triage.txt; python3 /opt/data/scripts/workload_triage.py > "$OUT"',
            "unhealthy: 0\nsystem unhealthy: 0",
            agent=SEEDED_B,
        ),
        _worker_call(
            "card-b",
            11.5,
            "python3 -c \"print(open('/tmp/workload_triage.txt').read())\"",
            "unhealthy: 0\nsystem unhealthy: 0",
            agent=SEEDED_B,
        ),
        _worker_call("card-c", 10.2, "command -v /opt/data/scripts/workload_triage.py", "/opt/data/scripts/workload_triage.py", agent=SEEDED_C),
        _worker_call("card-c", 11.2, f"command -V /opt/data/scripts/workload_triage.py && {SCAN_CMD}", "unhealthy: 0\nsystem unhealthy: 0", agent=SEEDED_C),
        _worker_call(
            "card-b",
            12.0,
            "kubectl exec payments-api-7f8 -n seeded-debug -- curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/healthz",
            "200",
            agent=SEEDED_B,
        ),
        _worker_call(
            "card-c",
            12.5,
            "kubectl exec payments-api-7f8 -n seeded-debug -- ls -Al /data",
            "total 0",
            agent=SEEDED_C,
        ),
        _worker_call(
            "card-c",
            12.8,
            "kubectl exec payments-api-7f8 -n seeded-debug -- grep -A 3 Error /var/log/app.log",
            "",
            agent=SEEDED_C,
        ),
        _worker_call(
            "card-b",
            13.0,
            "kubectl get pods -n seeded-debug -o wide --field-selector spec.nodeName=gke-gga-seeded-b-default-pool-1234",
            "NAME READY",
            agent=SEEDED_B,
        ),
        _worker_call(
            "card-c",
            14.0,
            "kubectl get --raw /api/v1/namespaces/seeded-debug/pods",
            "{}",
            agent=SEEDED_C,
        ),
    ]
    assert _status(NO_POLL_OR_EXPLORE, calls) == "pass"
    assert _status(NO_RECHECK, calls) == "pass"
