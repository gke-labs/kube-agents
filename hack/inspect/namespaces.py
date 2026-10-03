from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample

from k8s_pod import kubectl_ok  # also registers the k8s-pod sandbox
from harness import harness_agent
from scoring import names_exactly

SANDBOX = Path(__file__).parent / "environments/kubectl/sandbox.yaml"
ANSWER_PATH = "answer.md"
NAMESPACE_NAMES = "jsonpath={.items[*].metadata.name}"


async def listed() -> list[str]:
    return (await kubectl_ok("get", "namespaces", "-o", NAMESPACE_NAMES)).split()


@task
def namespaces(harness: str = "gemini", skills: bool = True, identity: str | None = None):
    """Verifies that the agent lists the cluster's namespaces with kubectl."""
    return Task(
        dataset=[
            Sample(
                input=f"Which namespaces exist in the Kubernetes cluster? Write their names to `{ANSWER_PATH}`, one per line.",
            )
        ],
        solver=harness_agent(harness, skills, identity),
        scorer=names_exactly(answer_path=ANSWER_PATH, expected=listed),
        sandbox=("k8s-pod", str(SANDBOX)),
    )
