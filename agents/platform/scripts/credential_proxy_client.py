#!/usr/bin/env python3
"""Submit a supported CLI argv vector to the paired credential proxy."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


SUPPORTED_EXECUTABLES = ("kubectl", "gcloud", "gh", "git")


class TokenUnavailable(Exception):
    """The configured caller token could not be read."""


def authorization_headers() -> dict[str, str]:
    """Return the credential that identifies this caller to the broker.

    Empty when CREDENTIAL_PROXY_TOKEN_FILE is unset, which is the sidecar
    deployment: there the broker is reachable only on the Pod's own loopback,
    behind a socket only its own container can open, and it asks for no
    credential. When the broker runs in its own Pod the operator projects a
    ServiceAccount token with the broker's audience into this container and
    points this variable at it.

    Read on every invocation, never cached: the kubelet rewrites a projected
    token in place as it approaches expiry, and this process is short-lived
    enough that re-reading costs nothing.
    """
    token_file = os.environ.get("CREDENTIAL_PROXY_TOKEN_FILE", "").strip()
    if not token_file:
        return {}
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenUnavailable(f"{token_file}: {exc.strerror or exc}") from exc
    if not token:
        raise TokenUnavailable(f"{token_file} is empty")
    return {"Authorization": f"Bearer {token}"}

# Only these read KUBECONFIG: kubectl to pick a context, gcloud to write one in
# `container clusters get-credentials`. `git` and `gh` ignore the variable, so
# forwarding it to them buys nothing and costs plenty — the server rejects an
# out-of-workspace path rather than ignoring it, which would turn a stray
# KUBECONFIG into a 400 on a command that has nothing to do with Kubernetes.
KUBECONFIG_AWARE = frozenset({"kubectl", "gcloud"})


def execute(
    endpoint: str,
    argv: list[str],
    stdin: str | None = None,
) -> int:
    request_payload = {
        "requestId": str(uuid.uuid4()),
        "argv": argv,
        "cwd": os.getcwd(),
    }
    # The command runs in the sidecar, so the caller's environment is not
    # inherited. KUBECONFIG is the one variable an agent legitimately needs to
    # steer: Cluster Agent profiles pin themselves to a target cluster with it
    # (see agents/cluster/config.yaml). Forward the path and let the server
    # decide whether it is acceptable — it only honours paths inside the shared
    # workspace. Whitespace is stripped because profile .env files routinely
    # carry a trailing newline.
    if argv and argv[0] in KUBECONFIG_AWARE:
        kubeconfig = os.environ.get("KUBECONFIG", "").strip()
        if kubeconfig:
            request_payload["kubeconfig"] = kubeconfig
    if stdin is not None:
        request_payload["stdin"] = stdin
    body = json.dumps(
        request_payload,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        headers.update(authorization_headers())
    except TokenUnavailable as exc:
        # Sending the request anyway would earn an undifferentiated 401 and
        # hide the real fault, which is a broken token projection.
        print(f"credential proxy token unavailable: {exc}", file=sys.stderr)
        return 1
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/exec",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        payload = json.load(exc)
        if payload.get("code") == "SECURITY_POLICY_BLOCKED":
            print(
                payload.get("message", "Command blocked for security reasons."),
                file=sys.stderr,
            )
            print(f"policy rule: {payload.get('rule', 'unknown')}", file=sys.stderr)
            return 126
        print(payload.get("error", str(exc)), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"credential proxy unavailable: {exc.reason}", file=sys.stderr)
        return 1

    sys.stdout.write(payload.get("stdout", ""))
    sys.stderr.write(payload.get("stderr", ""))
    if payload.get("truncated"):
        print("credential proxy output truncated", file=sys.stderr)
    return int(payload.get("exitCode", 1))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint",
        default=os.getenv("CREDENTIAL_PROXY_URL"),
        required=os.getenv("CREDENTIAL_PROXY_URL") is None,
    )
    parser.add_argument(
        "executable",
        choices=SUPPORTED_EXECUTABLES,
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser.parse_args()


if __name__ == "__main__":
    invoked_as = os.path.basename(sys.argv[0])
    if invoked_as in set(SUPPORTED_EXECUTABLES):
        endpoint = os.getenv("CREDENTIAL_PROXY_URL")
        if endpoint is None:
            print("CREDENTIAL_PROXY_URL is not configured", file=sys.stderr)
            raise SystemExit(1)
        argv = [invoked_as, *sys.argv[1:]]
        # Do not consume inherited stdin here. MCP and other stdio-based parent
        # processes may have a protocol stream on fd 0. Pipelines and
        # redirections remain local to the sandbox shell around this wrapper.
        stdin = None
    else:
        args = parse_args()
        endpoint = args.endpoint
        argv = [args.executable, *args.arguments]
        stdin = None
    raise SystemExit(
        execute(
            endpoint,
            argv,
            stdin=stdin,
        )
    )
