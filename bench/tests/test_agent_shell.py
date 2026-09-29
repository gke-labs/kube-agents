"""``AGENT_POD`` points the agent-disk reads at a pod rather than the Service."""

from pathlib import Path

import pytest

from kube_agents_bench import harness


def test_agent_pod_is_the_exec_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text('#!/bin/sh\necho "$@"\n')
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("AGENT_POD", "eval-0")
    monkeypatch.delenv("AGENT_NAMESPACE", raising=False)
    monkeypatch.delenv("AGENT_CLUSTER_CONTEXT", raising=False)

    out = harness._agent_shell("true", 5)

    assert out == "exec pod/eval-0 -n kubeagents-system -c platform-agent -- sh -c true\n"
