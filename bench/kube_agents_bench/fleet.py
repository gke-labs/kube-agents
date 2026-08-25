# Copyright 2026 The Kubernetes Authors.
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

"""Resolving a seeded-fleet fixture ROLE to the kubeconfig that reaches it.

A case addresses the standing fleet (``bench/tf/fleet/``) by the role a
fixture plays -- ``crashloop-workload``, ``idle-nodepool``, ``drift-outlier``
-- and never by cluster name or project id. Every eval project carries its own
trio of seeded clusters, so a check naming ``seeded-a`` in project X is a check
that cannot run in project Y, and the pool of eval projects is meant to grow.

The mapping from role to cluster is NOT here. It lives in
``bench/tf/fleet/fixtures.json`` beside the Terraform that plants the fixtures,
and at run time ``hack/fleet-kubeconfigs.sh`` is the only thing that reads it:
for each role it writes ``<dir>/<role>.kubeconfig`` holding credentials for
whichever cluster of the leased project's trio carries that role. This module's
whole job is the last hop -- role name to file path -- which keeps the
resolution single-sourced and makes this side testable without a cloud.

The one rule that matters: an unresolvable role RAISES. It never returns None
and it never returns the ambient kubeconfig. Silently falling back to the
ambient config is precisely the defect this module exists to remove (blocker
A5 in ``bench/tasks/DRAFTS.md``): the ambient config points at the agent's host
cluster, which has none of the seeded namespaces, so a safeguard reading it
answers a question nobody asked.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "FLEET_KUBECONFIG_DIR_ENV",
    "ROLE_PATTERN",
    "FleetRoleUnresolved",
    "available_roles",
    "confirmed_subjects",
    "kubeconfig_for_role",
    "provisioned_project",
]

# Set by hack/fleet-kubeconfigs.sh, exported by hack/ci-eval-pr.sh.
FLEET_KUBECONFIG_DIR_ENV = "BENCH_FLEET_KUBECONFIG_DIR"

# A role name becomes a path segment. Anchoring it to lowercase-and-hyphens is
# what keeps `../../../root/.kube/config` from being a legal role, and it is
# enforced at spec-load time (the verifier's field validator) rather than only
# here, so a bad name fails before the run starts.
ROLE_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

_SUFFIX = ".kubeconfig"

# Written by hack/fleet-kubeconfigs.sh as `project=<id>`. The pool of eval
# projects is leased at random and not every project in it necessarily carries
# the fleet, so "role unavailable" is only actionable once it says where the
# runner looked.
_CONTEXT_FILE = ".fleet-context"

# Written by hack/fleet-kubeconfigs.sh beside each role's kubeconfig: one
# canonical subject per line, in the catalog's `<kind>/<name>` or
# `<kind>?<selector>` form, for every object the runner SAW on that cluster
# before the agent started.
_CONFIRMED_SUFFIX = ".confirmed"


class FleetRoleUnresolved(LookupError):
    """No kubeconfig exists for the named fixture role.

    Carries a message written for whoever reads the failing check's reason,
    because that is the only place it surfaces: which role was asked for, which
    roles the runner actually provisioned, and the two reasons the list is
    short (the runner never ran, or that cluster was unreachable).
    """


def available_roles(directory: str | os.PathLike[str] | None = None) -> list[str]:
    """Roles the runner provisioned, sorted. Empty when it never ran."""
    root = directory if directory is not None else os.environ.get(FLEET_KUBECONFIG_DIR_ENV)
    if not root:
        return []
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return []
    names = (p.name[: -len(_SUFFIX)] for p in entries if p.is_file() and p.name.endswith(_SUFFIX))
    # A file named exactly ".kubeconfig" would otherwise contribute an empty
    # string to the "roles available" list an operator reads off a failure.
    return sorted(name for name in names if name)


def provisioned_project(directory: str | os.PathLike[str] | None = None) -> str | None:
    """The project the runner fetched fleet credentials from, if it recorded one."""
    root = directory if directory is not None else os.environ.get(FLEET_KUBECONFIG_DIR_ENV)
    if not root:
        return None
    try:
        text = (Path(root) / _CONTEXT_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "project" and value.strip():
            return value.strip()
    return None


def confirmed_subjects(
    role: str, directory: str | os.PathLike[str] | None = None
) -> frozenset[str]:
    """Objects the runner SAW on ``role``'s cluster before the agent started.

    This is the only thing that entitles a check to call a later absence a
    violation. If ``deployment/payments-api`` is in here and is gone at check
    time, it went missing during the run and the run is answerable for it. If
    it is not in here, the runner never saw it, so its absence says the fixture
    was never planted -- an ``error`` about the environment, not a ``fail``
    charged to the agent.

    Args:
        role: A fixture role from ``bench/tf/fleet/fixtures.json``.
        directory: Override for the provisioned directory; defaults to
            ``$BENCH_FLEET_KUBECONFIG_DIR``.

    Returns:
        Canonical subject strings (``namespace/seeded-debug``,
        ``deployment/payments-api``, ``node?cloud.google.com/...``). Empty when
        the runner never ran, confirmed nothing, or the role declares no
        probes -- in every one of which cases nothing may be blamed on the run.
    """
    if not ROLE_PATTERN.fullmatch(role):
        return frozenset()
    root = directory if directory is not None else os.environ.get(FLEET_KUBECONFIG_DIR_ENV)
    if not root:
        return frozenset()
    try:
        text = (Path(root) / f"{role}{_CONFIRMED_SUFFIX}").read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


def kubeconfig_for_role(role: str, directory: str | os.PathLike[str] | None = None) -> str:
    """Path to the kubeconfig reaching the cluster that carries ``role``.

    Args:
        role: A fixture role from ``bench/tf/fleet/fixtures.json``.
        directory: Override for the provisioned directory; defaults to
            ``$BENCH_FLEET_KUBECONFIG_DIR``.

    Returns:
        An existing kubeconfig path.

    Raises:
        FleetRoleUnresolved: The runner provisioned no fleet kubeconfigs, or
            none for this role. Never falls back to the ambient kubeconfig.
    """
    if not ROLE_PATTERN.fullmatch(role):
        raise FleetRoleUnresolved(
            f"fixture role {role!r} is not a lowercase-hyphen name, so it "
            "cannot name a file the runner wrote"
        )

    root = directory if directory is not None else os.environ.get(FLEET_KUBECONFIG_DIR_ENV)
    if not root:
        raise FleetRoleUnresolved(
            f"no seeded-fleet kubeconfigs: {FLEET_KUBECONFIG_DIR_ENV} is unset, so "
            f"nothing fetched credentials for the cluster carrying fixture role "
            f"{role!r}. The runner must call hack/fleet-kubeconfigs.sh before the "
            "task loop; this check is NOT falling back to the ambient kubeconfig, "
            "which points at the agent's host cluster and carries no fixture."
        )

    path = Path(root) / f"{role}{_SUFFIX}"
    if not path.is_file():
        provisioned = available_roles(root)
        project = provisioned_project(root)
        where = f"project {project}" if project else "the leased project"
        raise FleetRoleUnresolved(
            f"no kubeconfig for fixture role {role!r} in {root}: before the run "
            f"started, the runner found no reachable cluster carrying a planted "
            f"{role!r} fixture in {where}. Either the seeded fleet "
            f"(bench/tf/fleet/) was never applied there, or its apply stopped "
            f"before planting this fixture, or that cluster could not be reached, "
            f"or the role is absent from bench/tf/fleet/fixtures.json. This is a "
            f"statement about the environment, not about the run. Roles available: "
            f"{provisioned or 'none'}."
        )
    return str(path)
