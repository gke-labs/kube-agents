"""inspect-repository-unmanaged-and-context-read's route check, against the verifier.

The case guards the snapshot route: a worker reads both repositories through
`inspect_repository.py clone`, and no command uses a broker workspace handle.
These run the case's own check through `WorkerCommandsVerifier` on worker log
lines as hermes renders them: the snapshot route passes, with or without a
direct download beside it, a leading `cd` and a chain the clone starts do not
hide it, and the red's workspace route fails.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "inspect-repository-unmanaged-and-context-read"
    / "task.yaml"
)
CHECK = "the-snapshot-route-and-no-workspace-handle"
SCRIPT = "/opt/data/profiles/platform/skills/inspect-repository/scripts/inspect_repository.py"
CLONE_PUBLIC = f"python3 {SCRIPT} clone --repo gke-labs/kube-agents --ref 0.13.0 --prefix tags.env --into ./kube-agents"
CLONE_CONTEXT = f"python3 {SCRIPT} clone --repo kubernetes-sigs/kind --ref v0.24.0 --into ./kind"


def _passes(*rows: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    transcript.set("", [], worker_commands=[{"task": "t_1", "command": row} for row in rows])
    try:
        result = WorkerCommandsVerifier(**check["check"]).verify(5.0)
    finally:
        transcript.clear()
    assert result.status in ("pass", "fail"), result.reason
    return result.success


def test_the_snapshot_route_passes():
    assert _passes(CLONE_PUBLIC, CLONE_CONTEXT, "cat ./kind/pkg/apis/config/defaults/image.go")


def test_a_direct_download_beside_the_snapshot_route_still_passes():
    # The first-move download exists on main; the follow-up issue owns it.
    assert _passes("curl -s https://raw.githubusercontent.com/gke-labs/kube-agents/0.13.0/tags.env", CLONE_PUBLIC)


def test_a_clone_after_cd_or_at_the_head_of_a_chain_is_seen():
    # hermes skips a leading `cd` and keeps a chain's first command.
    assert _passes(CLONE_PUBLIC + " + 1 command(s)")
    assert _passes(CLONE_CONTEXT)


def test_the_red_s_workspace_route_fails():
    opened = f"H=$(python3 {SCRIPT} open --repo gke-labs/kube-agents --depth 1)"
    assert not _passes(opened, "curl -s https://raw.githubusercontent.com/gke-labs/kube-agents/0.13.0/tags.env")
    assert not _passes(opened, CLONE_PUBLIC)


def test_no_clone_at_all_fails():
    assert not _passes("curl -s https://raw.githubusercontent.com/gke-labs/kube-agents/0.13.0/tags.env")
