#!/usr/bin/env python3
"""Let mcp-remote authenticate to the credential proxy instead of to Google.

Run by ``deploy/docker/Dockerfile`` against the mcp-remote clone, after
``npm install`` and before ``npm run build``.

The pinned fork exists for one feature: ``src/proxy.ts`` mints a Google
access token from Application Default Credentials on every request, and sets
``X-Goog-User-Project`` from the same lookup. In the gateway pod that ADC is
the install's service account, reached through Workload Identity, so every
``mcp__gke__*`` call carried the install's whole IAM authority and no policy
saw it. The agent configs now point each remote MCP server at the credential
proxy's ``/v1/mcp/<server>`` route, which checks the call against a per-tool
allowlist and forwards it on the broker's own credential. What the proxy needs
from mcp-remote is the gateway's projected ServiceAccount token, not a Google
one.

With ``MCP_REMOTE_BEARER_FILE`` set, this patch makes ``proxy.ts``:

- read the bearer from that file on every request, because the kubelet rotates
  a projected token in place. A missing or empty file throws, which the SDK
  turns into a failed send and ``apply_mcp_remote_forward_errors.py`` turns
  into an immediate JSON-RPC error: a misconfigured child fails the call, and
  never falls back to Google ADC or to an OAuth browser flow;
- skip the project lookup, because the proxy sets ``X-Goog-User-Project`` from
  its own credential (and ``gcloud`` is not in this image to fall back to);
- skip OAuth discovery, whose probes would reach the proxy unauthenticated and
  be logged there as rejected callers.

Unset, the fork behaves exactly as before. The variable is a path, not a
secret: the configs pass ``${CREDENTIAL_PROXY_TOKEN_FILE}``.

Usage::

    python3 apply_mcp_remote_bearer_file.py [MCP_REMOTE_ROOT]  # /opt/mcp-remote
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "src/proxy.ts"

PIN_NOTE = "mcp-remote changed — re-derive the anchors before advancing the pin."

# Asserted in the built bundle by the Dockerfile, so a patch that silently stops
# applying fails the image build instead of shipping a child that reaches
# Google on the gateway's own credential.
BUILD_MARKER = "MCP_REMOTE_BEARER_FILE"

IMPORT_ANCHOR = "import { GoogleAuth } from 'google-auth-library'\n"
IMPORT_PATCHED = (
    IMPORT_ANCHOR
    + "// kube-agents patch: see deploy/docker/patches/apply_mcp_remote_bearer_file.py\n"
    + "import { readFileSync } from 'fs'\n"
)

TOKEN_ANCHOR = """\
async function getGcloudAccessToken() {
  try {
"""
TOKEN_PATCHED = """\
// kube-agents patch: the credential proxy's token, when the config names one.
const BEARER_FILE = process.env.MCP_REMOTE_BEARER_FILE || ''

async function getGcloudAccessToken() {
  if (BEARER_FILE) {
    // Read on every call: the kubelet rewrites a projected token in place. A
    // throw here fails the send, rather than falling back to ADC or to OAuth.
    const token = readFileSync(BEARER_FILE, 'utf8').trim()
    if (!token) {
      throw new Error('MCP_REMOTE_BEARER_FILE names an empty file')
    }
    return token
  }
  try {
"""

PROJECT_ANCHOR = """\
async function getGcloudProjectId() {
  try {
"""
PROJECT_PATCHED = """\
async function getGcloudProjectId() {
  // kube-agents patch: the credential proxy sets X-Goog-User-Project itself.
  if (BEARER_FILE) {
    return null
  }
  try {
"""

DISCOVERY_ANCHOR = "  const discoveryResult = await discoverOAuthServerInfo(serverUrl, headers)\n"
DISCOVERY_PATCHED = """\
  // kube-agents patch: the credential proxy is not an OAuth resource server.
  const discoveryResult: Awaited<ReturnType<typeof discoverOAuthServerInfo>> = BEARER_FILE
    ? { authorizationServerUrl: serverUrl }
    : await discoverOAuthServerInfo(serverUrl, headers)
"""

SUBSTITUTIONS = (
    ("import", IMPORT_ANCHOR, IMPORT_PATCHED),
    ("token", TOKEN_ANCHOR, TOKEN_PATCHED),
    ("project", PROJECT_ANCHOR, PROJECT_PATCHED),
    ("discovery", DISCOVERY_ANCHOR, DISCOVERY_PATCHED),
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="mcp_remote_bearer_file", note=PIN_NOTE)
    patch.refuse_if_patched(BUILD_MARKER)
    for label, anchor, replacement in SUBSTITUTIONS:
        patch.substitute(anchor, replacement, label=label)
    # TypeScript: there is no ast.parse gate to run, `npm run build` is it.
    patch.commit(f"{len(SUBSTITUTIONS)} anchors", parse=False)


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/mcp-remote"))
