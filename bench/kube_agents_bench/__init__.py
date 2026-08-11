"""devops-bench agent harness for the kube-agents platform agent.

This package plugs the in-cluster platform agent into the devops-bench eval
harness as ``--agent-type kubeagents``. It replaces the legacy evaluator baked
into the eval container image: devops-bench is consumed as a pip-installed
library (pinned git SHA) and the agent transport lives here, so kube-agents
and devops-bench ship independently.
"""

from kube_agents_bench.harness import KubeAgentsHarness

__all__ = ["KubeAgentsHarness"]
