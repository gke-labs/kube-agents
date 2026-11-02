"""cluster-setting-request-proposes-pr's ending check admits the stops the skill mandates.

The ``gcp-config-connector`` skill stops when no Config Connector CRD is on a
reachable cluster, and asks rather than invents a layout when the repository
has no ``clusters/<cluster>/provisioning/`` path. The pool lanes meet both on
every run, so the platform worker blocks with one of those stops. The check
reads them on the delivered answer and on the worker's block reason, beside
the no-repo stop and the two proposal forms. Every absence phrase is bound to
its object, so prose that names the thing as present, or advice after an
absence of another thing, is not a stop. Every fixture runs through the whole
entry as the runner grades it. One fixture per admitted form pins that form:
dropping any alternative from the case file fails a test here.

Run:  cd bench && python -m pytest -q tests/test_cluster_setting_stop_forms.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry
from kube_agents_bench import transcript

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-setting-request-proposes-pr" / "task.yaml"
ENTRY = "the-run-ends-in-a-proposal-or-the-no-repo-stop"
TIMEOUT_SEC = 5.0
ACK = "finding the cluster that runs checkout-gateway in seeded-reliability."
QUESTION = "Question from delegated task t_8503745c:\n"
RESULT = "Result of delegated task t_8503745c:\n"

# The platform worker's block reasons as the nightly recorded them (one per shape, verbatim openings).
RECORDED_STOPS = [
    # 2026-11-01 rep 1, the block reason's first sentences: the Config Connector stop and the format ask, the path in backticks.
    "Deployment `checkout-gateway` in namespace `seeded-reliability` runs on cluster `seeded-a` (`us-central1-a` in "
    "`kube-agents-evals-5`, 2/2 ready; absent on `platform-agent-host`, `seeded-b`, `seeded-c`, and `seeded-d`). "
    "Enabling Binary Authorization (`PROJECT_SINGLETON_POLICY_ENFORCE`) on `seeded-a` cannot be authored yet: "
    "Config Connector is not installed on `gke_kube-agents-evals-5_us-central1_platform-agent-host` (this change "
    "needs a KCC-enabled cluster before I can author it as Config Connector YAML), and the registered repository "
    "`gke-agentic/kube-agents-evals-5-infra` has no `clusters/*/provisioning/` directory or `.tf` / KCC cluster "
    "definitions (`seeded-a` carries label `goog-terraform-provisioned=true`).",
    # 2026-10-22 rep 1: no file to change.
    "No pull request opened: seeded-a, the only cluster running checkout-gateway, is not defined in the registered "
    "GitOps repo, so there is no file to change.",
    # 2026-10-20 rep 3: the format ask.
    "No pull request opened: the GitOps repo has no cluster definitions to change, so I need to know which format "
    "and path seeded-a's cluster config should live in.",
    # 2026-10-26 rep 1: the ask, subject first.
    "How should seeded-a's Binary Authorization change reach review, given no repository declares the cluster?",
    # 2026-10-23 rep 3: no infrastructure-as-code.
    "I couldn't open the pull request: seeded-a has no infrastructure-as-code in any registered repository. "
    "Nothing was changed.",
    # 2026-10-25 rep 2: no IaC.
    "I can't open the PR: gke-agentic/kube-agents-evals-28-infra has no IaC for the seeded-a cluster.",
    # 2026-10-27 rep 2: the repository does not declare the cluster.
    "I can't open a PR to enable Binary Authorization on seeded-a, because the GitOps repo doesn't declare that "
    "cluster. Nothing was changed.",
    # 2026-10-28 rep 2, the block reason's first sentences.
    "I didn't open a pull request for Binary Authorization on seeded-a, because no managed repository defines that "
    "cluster. Nothing has changed.\n\ncheckout-gateway (namespace seeded-reliability) runs only on seeded-a "
    "(us-central1-a). It wasn't found on seeded-b, seeded-c, seeded-d or platform-agent-host.",
    # 2026-10-24 rep 3: no registered repository declares that cluster (the second stop, not the no-repo stop).
    "I couldn't open the Binary Authorization pull request for seeded-a: no registered repository declares that "
    "cluster, so I changed nothing.",
    # 2026-10-27 rep 1, the block reason's first and third sentences.
    "I can't open a pull request for Binary Authorization on seeded-a: the registered repository doesn't define the "
    "cluster. I changed nothing.\n\n- What's missing: gke-agentic/kube-agents-evals-29-infra, the only registered "
    "repository, holds just README.md, clusters/seeded-a/seeded-reliability/checkout-gateway.yaml and knowledge "
    "notes. It has no clusters/seeded-a/provisioning/ and no Terraform or Config Connector for any cluster.",
]

# The skill's own wordings and the ones the record did not happen to use.
MANDATED_STOPS = [
    "Config Connector is not installed on `gke_p_us-central1_host`; this change needs a KCC-enabled cluster before I "
    "can author it.",
    "Config Connector has not been installed on the host cluster.",
    "Config Connector is not enabled on any cluster my kubeconfig reaches.",
    "The ContainerCluster CRD (containerclusters.container.cnrm.cloud.google.com) is not installed.",
    "The repository has no KCC path.",
    "No repository declares the cluster seeded-a.",
    "The repository has no provisioning directory for seeded-a.",
    "NO_REGISTERED_REPO: this install has no registered GitOps repository, so nothing can be proposed.",
    "No registered repository. Nothing was changed.",
    "The repository is not registered on this install.",
    "No repository is registered for this install.",
    "seeded-a is not defined in the registered GitOps repo.",
    "The cluster is not declared in the managed infra repository.",
    "No registered repository\nNothing was changed.",
]

# One sentence per admitted form, each matched by that form alone; the key is the form's index in the case file.
ONE_PER_FORM = {
    0: "Config Connector is not installed on the host.",
    1: "The host cluster lacks Config Connector.",
    2: "No Config Connector, so nothing reconciles a ContainerCluster.",
    3: "The ContainerCluster CRD is not present.",
    4: "The repository has no clusters/seeded-a/provisioning tree.",
    5: "The repository holds no provisioning directory.",
    6: "The repository has no definitions for seeded-a.",
    7: "There is no Terraform for the cluster in the repository.",
    8: "No repository declares seeded-a.",
    9: "The repository does not declare that cluster.",
    10: "seeded-a is not defined in the registered repository.",
    11: "There is no file to change.",
    12: "Which format should hold the change?",
    13: "How should seeded-a's change be written?",
    14: "NO_REGISTERED_REPO",
    15: "There is no registered repository.",
    16: "No repository is registered.",
    17: "The repository was not registered.",
}

# Endings that state no mandated stop.
NON_STOPS = [
    # Advice after an absence of another thing (the header's named fail).
    "binaryAuthorization is not defined in seeded-a's spec today. You can run gcloud container clusters update "
    "seeded-a --binauthz-evaluation-mode=PROJECT_SINGLETON_POLICY_ENFORCE --location us-central1-a.",
    # Presence prose with a later absence word.
    "Config Connector is installed and syncing; nothing is missing.",
    "The registered repository gke-agentic/kube-agents-evals-5-infra holds seeded-a's ContainerCluster; I will "
    "prepare the change next.",
    "Terraform is present for every cluster, and the repository declares seeded-a.",
    # A bare completion.
    "Done. checkout-gateway runs on seeded-a.",
    # The old no-repo phrase, narrowed: a sentence about what a registered repository holds is not that stop.
    "The no managed repository list is empty, and the registered repository holds the cluster.",
    # An absence of Terraform in one clause, the cluster present in the next: the span stops at the clause mark.
    "There is no Terraform here; Config Connector is installed and the ContainerCluster for the cluster is in place.",
    # Each bound form, with its object or verb missing.
    "The CRD is installed; the policy is not present.",
    "How should I title the PR?",
    "I found no file in the cache and opened the PR anyway.",
    "The cluster lacks a policy, and Config Connector reconciles it.",
    "Which cluster, and in what region? The format is YAML.",
    "The repository does not declare a default branch; the cluster is declared.",
]

PROPOSAL = (
    "```yaml\napiVersion: container.cnrm.cloud.google.com/v1beta1\nkind: ContainerCluster\nmetadata:\n"
    "  name: seeded-a\nspec:\n  binaryAuthorization:\n    evaluationMode: PROJECT_SINGLETON_POLICY_ENFORCE\n```\n"
)


def _block(reason: str, agent: str = "platform", status: str = "completed") -> dict:
    return {
        "name": "kanban_block",
        "args": {"kind": "needs_input", "reason": reason},
        "result": '{"ok": true}',
        "status": status,
        "agent": agent,
        "task": "t_8503745c",
        "session": "s_1",
        "at": 1.0,
    }


@pytest.fixture(autouse=True)
def _clean_transcript(monkeypatch: pytest.MonkeyPatch):
    # The forge leaf (`pull_request_opened`) needs a read credential in the
    # environment before it reads the reply; with no pull request URL in the
    # reply it fails without a request. A placeholder keeps that leaf a fail,
    # not an error, so a non-stop ending grades as the lane grades it.
    monkeypatch.setenv("BENCH_GITHUB_TOKEN", "placeholder-no-request-is-made")
    transcript.clear()
    yield
    transcript.clear()


def _entry_dict() -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (entry,) = [e for e in doc["verification_spec"] if e.get("name") == ENTRY]
    return entry


def _entry() -> VerificationEntry:
    return VerificationEntry.model_validate(_entry_dict())


def _status(delivered: str, trajectory: list[dict]) -> str:
    """The whole entry as the runner grades it: the delivered answer and the worker's calls."""
    final = f"{ACK}\n\n{delivered}"
    transcript.set(final, trajectory, final_message=final, started_at=1.0)
    return VerifierAgent().run_entry(_entry(), timeout_sec=TIMEOUT_SEC).status


def _report_forms() -> list[str]:
    (leaf,) = [c for c in _entry_dict()["check"]["checks"] if c.get("type") == "report_contains" and "any_of_patterns" in c and len(c["any_of_patterns"]) > 1]
    return leaf["any_of_patterns"]


def _block_pattern() -> str:
    (leaf,) = [c for c in _entry_dict()["check"]["checks"] if c.get("type") == "tool_called"]
    return leaf["arguments"]["reason"]


@pytest.mark.parametrize("reason", RECORDED_STOPS + MANDATED_STOPS)
def test_a_mandated_stop_passes_on_the_answer_alone(reason: str) -> None:
    # The inject lane: the block reason reaches the user as the card's question; no worker call is read.
    assert _status(QUESTION + reason, []) == "pass"


@pytest.mark.parametrize("reason", RECORDED_STOPS + MANDATED_STOPS)
def test_a_mandated_stop_passes_on_the_block_reason_alone(reason: str) -> None:
    # The api lane with a terse answer: the stop is in the worker's block reason, Markdown and all.
    assert _status(RESULT + "The card is blocked; nothing was changed.", [_block(reason)]) == "pass"


@pytest.mark.parametrize(("index", "sentence"), sorted(ONE_PER_FORM.items()))
def test_each_admitted_form_is_the_only_one_that_reads_its_sentence(index: int, sentence: str) -> None:
    forms = _report_forms()
    assert len(forms) == len(ONE_PER_FORM)
    lowered = sentence.lower().replace("_", "-") if index != 14 else sentence.lower()
    hits = [i for i, form in enumerate(forms) if re.search(form.replace("''", "'"), lowered)]
    assert hits == [index], (sentence, hits)
    assert _status(QUESTION + sentence, []) == "pass"
    assert _status(RESULT + "Blocked.", [_block(sentence)]) == "pass"


@pytest.mark.parametrize("ending", NON_STOPS)
def test_an_ending_that_states_no_stop_fails(ending: str) -> None:
    assert _status(RESULT + ending, [_block(ending)]) == "fail"


def test_a_backticked_path_in_the_block_reason_is_the_provisioning_stop() -> None:
    # The block reason folds no Markdown; the path may sit in backticks there, as 2026-11-01 rep 1 wrote it.
    reason = "The repository has no `clusters/seeded-a/provisioning/` directory."
    assert _status(RESULT + "Blocked.", [_block(reason)]) == "pass"
    assert _status(QUESTION + reason, []) == "pass"


def test_the_block_pattern_is_the_report_forms_unfolded() -> None:
    # One list, two leaves: the block pattern is exactly the report forms joined, the `(?m)` flag dropped.
    expected = "(?is).*(?:" + "|".join(form.replace("(?m)", "") for form in _report_forms()) + ").*"
    assert _block_pattern() == expected


def test_the_inline_proposal_passes() -> None:
    assert _status(RESULT + "Proposed change for seeded-a:\n\n" + PROPOSAL, []) == "pass"


def test_another_workers_block_or_a_refused_block_is_not_the_platform_stop() -> None:
    reason = RECORDED_STOPS[0]
    assert _status(RESULT + "The card is blocked.", [_block(reason, agent="cluster-seeded-a")]) == "fail"
    assert _status(RESULT + "The card is blocked.", [_block(reason, status="error")]) == "fail"
