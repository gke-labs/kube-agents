"""Repository-owned identity contract for the stock admin-portal target."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELM_VALUES = REPO_ROOT / "charts" / "kube-agents" / "values.yaml"

_K8S_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


def _mapping_value(path: Path, section: str, key: str) -> str:
    """Read one scalar from a top-level YAML mapping without template expansion."""

    active_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            active_section = line.strip() == f"{section}:"
            continue
        if active_section and indent == 2:
            name, separator, value = line.strip().partition(":")
            if separator and name == key:
                return value.strip().strip("'\"")
    raise RuntimeError(f"{path} does not define {section}.{key}")


@cache
def canonical_platform_agent_name() -> str:
    """Return the stock name from the canonical Helm install values."""

    return helm_platform_agent_name()


class CanonicalPlatformAgentMissing(RuntimeError):
    """The connected target does not contain the stock installation resource."""


def select_canonical_platform_agent(payload: dict) -> str:
    """Select the stock resource from a live PlatformAgent list."""

    expected = canonical_platform_agent_name()
    discovered = sorted(
        {
            str((item.get("metadata") or {}).get("name") or "")
            for item in payload.get("items", [])
        }
    )
    discovered = [name for name in discovered if _K8S_NAME.fullmatch(name)]
    if expected not in discovered:
        names = ", ".join(discovered) if discovered else "none"
        raise CanonicalPlatformAgentMissing(
            f"Canonical PlatformAgent {expected} was not found; "
            f"discovered PlatformAgent resources: {names}."
        )
    return expected


def helm_platform_agent_name() -> str:
    """Return the Helm installation default used by source-parity tests."""

    name = _mapping_value(HELM_VALUES, "platformAgent", "name")
    if not _K8S_NAME.fullmatch(name):
        raise RuntimeError(f"{HELM_VALUES} has an invalid platformAgent.name")
    return name


@dataclass(frozen=True, order=True)
class GatewayEndpoint:
    """One live gateway pod and the container that owns its API port."""

    pod: str
    container: str


def gateway_endpoints(payload: dict) -> tuple[GatewayEndpoint, ...]:
    """Resolve gateway endpoints from operator-produced Kubernetes pod specs."""

    endpoints: list[GatewayEndpoint] = []
    for item in payload.get("items", []):
        pod = str((item.get("metadata") or {}).get("name") or "")
        candidates = []
        for container in (item.get("spec") or {}).get("containers", []):
            ports = container.get("ports") or []
            if any(port.get("name") == "api" for port in ports):
                name = str(container.get("name") or "")
                if _K8S_NAME.fullmatch(name):
                    candidates.append(name)
        if pod and len(candidates) == 1:
            endpoints.append(GatewayEndpoint(pod, candidates[0]))
    return tuple(sorted(endpoints))
