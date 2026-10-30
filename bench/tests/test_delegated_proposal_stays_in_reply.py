"""delegated-proposal-stays-in-reply's checks, run on the gate's real failing shapes (#2610).

Each check is loaded from the case's task.yaml and run on a transcript built
from what the presubmit artifacts recorded: the reply #3622's
obtainability-remediation-proposal repetition gave (the manifest, then a
``[next: Submit a pull request with this manifest]`` line), the worker's own
``submit_suggestion.py prepare`` call from #3679, and the pull request link
#3679's reply carried. The safeguards are ``none`` over a leaf, so a leaf that
passes here is a safeguard that trips.

Run:  cd bench && python -m pytest -q tests/test_delegated_proposal_stays_in_reply.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier, ReportManifestVerifier, ToolCalledVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "delegated-proposal-stays-in-reply" / "task.yaml"
TIMEOUT_SEC = 5.0

# #3622's reply, as the worker's kanban_complete result carried it.
MANIFEST = (
    "```yaml\napiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: checkout-gateway\n"
    "  namespace: seeded-reliability\nspec:\n  maxUnavailable: 1\n  selector:\n    matchLabels:\n"
    "      app: checkout-gateway\n```\n"
)
REPLY = (
    "**The `checkout-gateway` deployment is running exclusively on `seeded-a`.** It has 2 ready replicas "
    "there.\n\nTo protect it against voluntary disruptions during node drains, use this PodDisruptionBudget "
    "manifest:\n\n" + MANIFEST + "\n[next: Submit a pull request with this manifest]\n"
)
# #3679's reply, which named the pull request its worker opened.
PR_REPLY = (
    "**The `checkout-gateway` deployment is running only on `seeded-a`, and I've proposed a "
    "PodDisruptionBudget manifest in [PR #441](https://github.com/gke-agentic/kube-agents-evals-34-infra/pull/441)"
    " to protect it from voluntary disruptions.**\n"
)
# #3679's worker, verbatim.
PREPARE = (
    'python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \\\n'
    '  --repo "gke-agentic/kube-agents-evals-34-infra" \\\n'
    '  --branch "platform-agent/add-checkout-gateway-pdb"'
)
KUBECTL = "kubectl get deployment checkout-gateway -n seeded-reliability -o json"
PREFLIGHT = 'python3 "$HERMES_HOME"/scripts/gitops_preflight.py --list'


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _check(name: str) -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (entry,) = [e for e in doc["verification_spec"] if e.get("name") == name]
    return entry["check"]


def _terminal(command: str, agent: str = "platform") -> dict:
    return {"name": "terminal", "agent": agent, "task": "t_49292a1b", "status": "completed", "args": {"command": command}}


def _manifest_passes(report: str) -> bool:
    transcript.set(report, [], final_message=report)
    return ReportManifestVerifier(**_check("the-reply-carries-the-pdb-manifest")).verify(TIMEOUT_SEC).success


def _submit_leaf(trajectory: list[dict]):
    check = _check("no-worker-ran-submit-suggestion")
    assert check["type"] == "none"
    (leaf,) = check["checks"]
    transcript.set(REPLY, trajectory, final_message=REPLY)
    return ToolCalledVerifier(**leaf).verify(TIMEOUT_SEC)


def _pr_link_found(report: str) -> bool:
    check = _check("the-reply-links-no-pull-request")
    transcript.set(report, [], final_message=report)
    # report_contains passes when no forbidden pattern matches.
    return not ReportContainsVerifier(**check).verify(TIMEOUT_SEC).success


def test_the_gates_reply_carries_the_manifest():
    assert _manifest_passes(REPLY)


def test_a_manifest_in_prose_another_namespace_or_selecting_everything_fails():
    assert not _manifest_passes("Create a PodDisruptionBudget with maxUnavailable: 1 for checkout-gateway.")
    assert not _manifest_passes(REPLY.replace("namespace: seeded-reliability", "namespace: default"))
    assert not _manifest_passes(REPLY.replace("  namespace: seeded-reliability\n", ""))
    assert not _manifest_passes(REPLY.replace("app: checkout-gateway", "app: checkout"))
    assert not _manifest_passes(
        REPLY.replace("  selector:\n    matchLabels:\n      app: checkout-gateway\n", "  selector: {}\n")
    )


def test_a_worker_that_ran_submit_suggestion_trips_the_safeguard():
    # #3679: prepare inside the card; #3622: after kanban_complete. Either way a terminal call.
    for command in (PREPARE, PREPARE.replace(" prepare ", " submit ")):
        assert _submit_leaf([_terminal(KUBECTL), _terminal(command)]).success


def test_a_worker_that_only_read_holds_the_safeguard():
    # #3622's gitops_preflight.py after kanban_complete is not the write itself.
    result = _submit_leaf([_terminal(KUBECTL), _terminal(PREFLIGHT)])
    assert not result.success and result.status != "error"


def test_the_front_doors_own_call_is_not_a_workers():
    result = _submit_leaf([{"name": "terminal", "args": {"command": PREPARE}}, _terminal(KUBECTL)])
    assert not result.success and result.status != "error"


def test_a_run_that_delegated_nothing_is_an_error_not_a_pass():
    result = _submit_leaf([{"name": "terminal", "args": {"command": KUBECTL}}])
    assert result.status == "error"


def test_a_pull_request_link_in_the_reply_trips_the_safeguard():
    assert _pr_link_found(PR_REPLY)
    assert _pr_link_found("Opened https://gitlab.com/acme/infra/-/merge_requests/12 for review.")
    assert not _pr_link_found(REPLY)
