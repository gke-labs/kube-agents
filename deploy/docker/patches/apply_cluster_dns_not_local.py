"""Classify a Kubernetes service DNS name as a cloud endpoint, not a local one.

One anchored edit in ``agent/model_metadata.py``: ``is_local_endpoint`` treats
any host ending in ``.local`` as local (RFC 6762 mDNS), and a Kubernetes
service's cluster DNS name ends in ``.svc.cluster.local``. So the agent's model
endpoint, the in-cluster inference gateway
(``inference-gateway.<namespace>.svc.cluster.local``), is classified as a
local server. Where no provider timeouts are configured, that flag switches
every model call's failure handling:

- the stream stale detector (no first chunk, or no chunk for a while) waits
  ``HERMES_LOCAL_STREAM_STALE_TIMEOUT`` (900s) instead of the cloud 180s;
- the stream read timeout is raised to the whole request timeout (1800s);
- the non-stream stale detector is switched off;
- local-server probes (Ollama ``/api/show`` and ``/api/tags``, llama.cpp
  ``/v1/props``, LM Studio ``/api/v1/models``) run at startup
  and for context-length resolution, and the gateway answers each with a 404;
- ``/models`` metadata is not disk-cached between processes.

A Gemini call that stalled for over ten minutes ran its full 628s without a
reconnect for this reason (gke-labs/kube-agents#2914). The image's configs now
set the gateway's timeouts explicitly (``providers.custom``), which bypasses the
local timeout branches; this patch removes the rest: most of the local-server
probes (the generic ``/api/show`` fallback still runs once), the uncached
``/models`` fetch, and the local-only load poller and image-routing check.

The edit puts an early ``return False`` for Kubernetes service DNS ahead of the
``.local`` rule, so a real mDNS host (``nas.local``), loopback, an unqualified
name and a private address stay local. The suffixes are matched on the parsed
hostname, which ``urllib`` lowercases.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import patchlib

METADATA_RELATIVE = "agent/model_metadata.py"

# Where the image installs Hermes, when no root is given.
DEFAULT_HERMES_ROOT = "/opt/hermes"

# The def whose body gains the early return. Located by AST.
LOCAL_DEF = "is_local_endpoint"

# The guard the inserted line follows, as upstream writes it, without its
# indentation: the hostname is known from here on.
HOST_NONE_GUARD = ("if host is None:", "    return False")

MARKER = "kube-agents patch: cluster-dns-not-local"

# Kubernetes service DNS: the short form (<svc>.<ns>.svc) and the cluster
# domain (cluster.local by default, which also covers <svc>.<ns>.svc.cluster.local).
CLUSTER_DNS_SUFFIXES = (".svc", ".cluster.local")

INSERTED_LINES = (
    f"if host.endswith({CLUSTER_DNS_SUFFIXES!r}):  # {MARKER}",
    "    return False",
)


def inserted(indent: str) -> str:
    """The inserted guard at the def's body indentation."""
    return "".join(indent + line + "\n" for line in INSERTED_LINES)


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, METADATA_RELATIVE, prefix="cluster-dns-not-local")
    patch.refuse_if_patched(MARKER)
    definition = patch.find_def(LOCAL_DEF, label="local endpoint classification")
    indent = definition.body_indent
    # Bounded by line breaks, so the guard is matched whole and only at the
    # def's own body indentation. The closing break is a lookahead so two
    # guards in a row, which share it, are both counted.
    anchor = "".join("\n" + indent + line for line in HOST_NONE_GUARD)
    body = "\n" + patch.source[definition.start : definition.end] + "\n"
    matches = list(re.finditer(re.escape(anchor) + r"(?=\n)", body))
    if len(matches) != 1:
        raise SystemExit(
            f"cluster-dns-not-local patch: {METADATA_RELATIVE}: expected 1 "
            f"'host is None' guard in {LOCAL_DEF}(), found {len(matches)}. {patch.note}"
        )
    # Just past the guard's own line break, where the next line begins.
    offset = definition.start + matches[0].end()
    patch.splice(offset, offset, inserted(indent))
    patch.commit("kubernetes service DNS classified as a cloud endpoint")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT))
