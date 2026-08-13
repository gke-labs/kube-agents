"""Evaluation clients for the kube-agents platform agent.

The devops-bench entry point imports :mod:`kube_agents_bench.harness` directly.
Keeping this package initializer side-effect free also lets the portal CUJ
evaluator run without importing devops-bench.
"""
