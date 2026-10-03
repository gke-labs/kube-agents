from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample

import k8s_pod  # noqa: F401  registers the k8s-pod sandbox
from harness import harness_agent
from scoring import names_exactly

SANDBOX = Path(__file__).parent / "environments/memory/sandbox.yaml"
ANSWER_PATH = "answer.md"
INVENTORY = ["kube-agents", "payments-prod", "orbital-7"]


@task
def clusters_from_memory(harness: str = "gemini", skills: bool = True, identity: str | None = None):
    """Verifies that the agent lists its managed clusters by consulting the inventory file."""
    return Task(
        dataset=[
            Sample(
                input=f"Which clusters do you manage? Write their names to `{ANSWER_PATH}`, one per line.",
                target=INVENTORY,
            )
        ],
        solver=harness_agent(harness, skills, identity),
        scorer=names_exactly(answer_path=ANSWER_PATH),
        sandbox=("k8s-pod", str(SANDBOX)),
    )
