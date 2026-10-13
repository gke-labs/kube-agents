#!/usr/bin/env python3
"""Build gate for the cluster-DNS classification patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_cluster_dns_not_local.py``. The applier proves its anchor matched once;
this proves the patched ``is_local_endpoint`` classifies what it should:

- the agent's own model endpoint (``deploy/shared/defaults/config.yaml``'s
  ``model.base_url`` shape) and the other forms of Kubernetes service DNS are
  NOT local, so Hermes skips its local-server handling for them;
- loopback, an unqualified name, a private address and a real mDNS ``.local``
  host stay local, so the patch is no wider than service DNS.

Usage::

    cd /opt/hermes && python3 verify_cluster_dns_not_local.py
"""

from __future__ import annotations

import sys

from agent.model_metadata import is_local_endpoint

# The inference gateway as the agent's config addresses it, and the other
# spellings of a Kubernetes service name.
NOT_LOCAL = (
    "http://inference-gateway.kubeagents-system.svc.cluster.local/v1",
    "http://inference-gateway.kubeagents-system.svc/v1",
    "http://inference-gateway.kubeagents-system.svc.cluster.local:8080/v1",
    "https://API.EXAMPLE.SVC.CLUSTER.LOCAL/v1",
    "https://generativelanguage.googleapis.com/v1beta",
)
STILL_LOCAL = (
    "http://localhost:11434/v1",
    "http://127.0.0.1:8080/v1",
    "http://ollama:11434/v1",
    "http://192.168.1.20:11434/v1",
    "http://nas.local:11434/v1",
)


def main() -> int:
    failures = [f"{url}: classified local" for url in NOT_LOCAL if is_local_endpoint(url)]
    failures += [f"{url}: no longer classified local" for url in STILL_LOCAL if not is_local_endpoint(url)]
    if failures:
        print("cluster-dns-not-local verify FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print(f"cluster-dns-not-local verify: {len(NOT_LOCAL)} service names cloud, {len(STILL_LOCAL)} local hosts local")
    return 0


if __name__ == "__main__":
    sys.exit(main())
