#!/usr/bin/env python3
"""Submit a supported CLI argv vector to the paired credential proxy."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


SUPPORTED_EXECUTABLES = ("kubectl", "gcloud", "gh", "git")

# How long to wait to reach the broker. Bounds the connect only — see
# BrokerConnection.
BROKER_CONNECT_TIMEOUT_SECONDS = 10.0


class BrokerConnection(http.client.HTTPConnection):
    """Bound how long we wait to reach the broker, not how long it works.

    A plain ``urlopen(request, timeout=N)`` sets one socket timeout for the
    whole exchange, which would put a ceiling on the command as well as on the
    connect. That ceiling must not exist: Envoy routes /v1/exec with
    ``timeout: 0s`` deliberately, because a proxied ``gcloud container clusters
    get-credentials`` or a large ``git clone`` legitimately runs for minutes.

    Before the split there was no need for either — the broker was on the Pod's
    own loopback, so a connect either succeeded or was refused at once. Now the
    call crosses a Service. A Pending broker still fails fast, with
    ``[Errno 111] Connection refused`` from a Service that has no endpoints;
    what hangs is a SYN that is dropped rather than rejected, which is exactly
    what a default-deny egress policy does. So: a timeout while connecting, and
    none once connected.
    """

    def connect(self) -> None:
        self.timeout = BROKER_CONNECT_TIMEOUT_SECONDS
        super().connect()
        self.sock.settimeout(None)


class _BrokerHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(BrokerConnection, req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect out of the broker.

    urllib re-sends the Authorization header across a cross-host redirect, so
    a 302 in a broker response would hand the projected token to wherever the
    Location points. Only reachable by something that already controls the
    broker's responses, but the header is the one thing worth not leaking on
    the way out.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


# A private opener rather than urllib.request.install_opener: this module is
# imported by github_token_refresh and the two relay patches, and a global
# opener would strip the total timeouts their own urlopen calls rely on.
_BROKER_OPENER = urllib.request.build_opener(_BrokerHTTPHandler, _NoRedirect())


def open_broker_request(request: urllib.request.Request):
    """Send `request` to the broker with a bounded connect."""
    return _BROKER_OPENER.open(request)


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
        with open_broker_request(request) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        # The proxy's own errors are JSON, but the error can also come from
        # whatever sits between shim and proxy — an Envoy restarting mid-request
        # answers 503 with an HTML body, and a traceback here turns a transient
        # sidecar blip into a shim crash the agent cannot read.
        try:
            payload = json.load(exc)
        except (ValueError, TypeError):
            print(
                f"credential proxy error (HTTP {exc.code}): non-JSON response",
                file=sys.stderr,
            )
            return 1
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
