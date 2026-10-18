# Copyright 2026 Google LLC
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

"""Inspect AI evaluation tasks for kube-agents conforming to eval-runner specifications."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
DATASET_PATH = CURRENT_DIR / "datasets" / "kube_agents_eval_dataset.jsonl"
DEFAULT_MODEL = "google/gemini-3.5-flash-lite"

try:
    from inspect_ai import Task, task
    from inspect_ai.dataset import MemoryDataset, Sample
    from inspect_ai.solver import generate, system_message
except ImportError:
    # Minimal fallback dummy decorator when inspect_ai is not installed
    def task(fn):
        return fn
    Task = Any
    MemoryDataset = Any
    Sample = Any
    generate = Any
    system_message = Any

try:
    from .scorers import kube_agents_cx_scorer
except ImportError:
    try:
        from scorers import kube_agents_cx_scorer
    except ImportError:
        kube_agents_cx_scorer = None


def _resolve_model():
    try:
        from inspect_ai.model._model import active_model
        m = active_model()
        if m is not None and str(m) != "none/none":
            return m
    except Exception:
        pass
    return DEFAULT_MODEL


def _load_samples_for_task(task_id: str) -> list[Sample]:
    samples = []
    if not DATASET_PATH.exists():
        return samples
    with open(DATASET_PATH, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("task_id") == task_id:
                samples.append(
                    Sample(
                        input=data["input"],
                        target=data.get("target", ""),
                        id=data["id"],
                        metadata={
                            "id": data["id"],
                            "task_id": data["task_id"],
                            "category": data.get("category", ""),
                            "required": data.get("required", []),
                            "forbidden": data.get("forbidden", []),
                            "min_length": data.get("min_length", 15),
                        },
                    )
                )
    return samples


@task
def identity_and_role() -> Task:
    """Evaluates model identity, role description, and permission boundaries."""
    sys_prompt = (
        "You are kube-agents, the conversational front door coordinator for Kubernetes fleet operations. "
        "You are backed by gemini-3.8-flash on Google Cloud Vertex AI (vertex_ai). "
        "You operate in a plan-and-delegate model with no direct CLI execution tools."
    )
    return Task(
        name="kubeagents/identity_and_role",
        model=_resolve_model(),
        dataset=MemoryDataset(_load_samples_for_task("kubeagents/identity_and_role")),
        solver=[system_message(sys_prompt), generate()],
        scorer=kube_agents_cx_scorer() if kube_agents_cx_scorer else None,
    )


@task
def fleet_cloud_scope() -> Task:
    """Evaluates GCP project scope and GKE multi-cluster fleet discovery."""
    sys_prompt = (
        "You are kube-agents coordinator. The active GCP project in scope is gca-gke-test. "
        "Host cluster is ka-webconsole-pr2321 in us-central1."
    )
    return Task(
        name="kubeagents/fleet_cloud_scope",
        model=_resolve_model(),
        dataset=MemoryDataset(_load_samples_for_task("kubeagents/fleet_cloud_scope")),
        solver=[system_message(sys_prompt), generate()],
        scorer=kube_agents_cx_scorer() if kube_agents_cx_scorer else None,
    )


@task
def cluster_inspection() -> Task:
    """Evaluates cluster state inspection (namespaces, node status, pods, K8s version)."""
    return Task(
        name="kubeagents/cluster_inspection",
        model=_resolve_model(),
        dataset=MemoryDataset(_load_samples_for_task("kubeagents/cluster_inspection")),
        solver=[generate()],
        scorer=kube_agents_cx_scorer() if kube_agents_cx_scorer else None,
    )


@task
def workload_inspection() -> Task:
    """Evaluates workload inspection (web-console replicas, services, CRDs, resource limits)."""
    return Task(
        name="kubeagents/workload_inspection",
        model=_resolve_model(),
        dataset=MemoryDataset(_load_samples_for_task("kubeagents/workload_inspection")),
        solver=[generate()],
        scorer=kube_agents_cx_scorer() if kube_agents_cx_scorer else None,
    )


@task
def diagnostics_and_logs() -> Task:
    """Evaluates cluster diagnostics, Warning events, and container log RBAC boundaries."""
    return Task(
        name="kubeagents/diagnostics_and_logs",
        model=_resolve_model(),
        dataset=MemoryDataset(_load_samples_for_task("kubeagents/diagnostics_and_logs")),
        solver=[generate()],
        scorer=kube_agents_cx_scorer() if kube_agents_cx_scorer else None,
    )


@task
def substrate_planning() -> Task:
    """Evaluates Agent Substrate recognition, two-part architecture, and /plan staging."""
    sys_prompt = (
        "You are kube-agents coordinator. When asked to install Agent Substrate, always structure "
        "a /plan separating the Cluster Part (operator, CRDs, node labels ate.dev/substrate-version) "
        "from the Workload Part (Worker compute pools, Actor definitions, pod execution sandboxes) "
        "prior to enabling or mutating infrastructure."
    )
    return Task(
        name="kubeagents/substrate_planning",
        model=_resolve_model(),
        dataset=MemoryDataset(_load_samples_for_task("kubeagents/substrate_planning")),
        solver=[system_message(sys_prompt), generate()],
        scorer=kube_agents_cx_scorer() if kube_agents_cx_scorer else None,
    )
