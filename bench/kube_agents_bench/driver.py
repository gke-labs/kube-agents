"""Interim CLI driver: register the harness, then run devops-bench.

Needed only while the pinned devops-bench SHA predates agent entry-point
discovery (the ``devops_bench.agents`` registry group). Importing
:mod:`kube_agents_bench.harness` registers ``kubeagents``; everything else is
delegated verbatim to the ``devops-bench`` CLI, so flags and env vars behave
identically:

    kube-agents-bench --source ./tasks --agent-type kubeagents

Once the pin advances past entry-point discovery, the stock ``devops-bench``
CLI resolves ``kubeagents`` on its own and this driver becomes a no-op alias.
"""

from __future__ import annotations

import kube_agents_bench.harness  # noqa: F401  - registers the "kubeagents" agent
from devops_bench.cli import main as _bench_main

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Run the devops-bench CLI with the ``kubeagents`` agent registered."""
    return _bench_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
