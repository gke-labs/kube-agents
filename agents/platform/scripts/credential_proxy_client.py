#!/usr/bin/env python3
"""Submit exact raw command text to the paired credential proxy."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import urllib.error
import urllib.request
import uuid


def kubernetes_context_from_env() -> dict[str, str] | None:
    values = {
        "contextName": os.getenv("KUBE_CONTEXT_NAME", ""),
        "projectId": os.getenv("GKE_PROJECT_ID", ""),
        "location": os.getenv("GKE_LOCATION", ""),
        "clusterName": os.getenv("GKE_CLUSTER_NAME", ""),
        "defaultNamespace": os.getenv("KUBE_DEFAULT_NAMESPACE", ""),
    }
    if not any(values.values()):
        return None
    missing = [key for key in ("contextName", "projectId", "location", "clusterName") if not values[key]]
    if missing:
        raise ValueError(
            "incomplete Kubernetes execution context: missing " + ", ".join(missing)
        )
    return values


def execute(
    endpoint: str,
    command: str,
    stdin: str | None = None,
    kubernetes_context: dict[str, str] | None = None,
) -> int:
    request_payload = {"requestId": str(uuid.uuid4()), "command": command}
    if kubernetes_context is not None:
        request_payload["context"] = {"kubernetes": kubernetes_context}
    if stdin is not None:
        request_payload["stdin"] = stdin
    body = json.dumps(
        request_payload,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/exec",
        data=body,
        headers={"Content-Type": "application/json"},
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
        "--context",
        dest="context_name",
        help="Kubernetes context name; project/location/cluster come from the environment.",
    )
    parser.add_argument(
        "--endpoint",
        default=os.getenv("CREDENTIAL_PROXY_URL"),
        required=os.getenv("CREDENTIAL_PROXY_URL") is None,
    )
    parser.add_argument(
        "command",
        nargs="?",
        help="Exact command text. If omitted, command text is read unchanged from stdin.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    invoked_as = os.path.basename(sys.argv[0])
    if invoked_as in {"kubectl", "gcloud", "gh", "git"}:
        endpoint = os.getenv("CREDENTIAL_PROXY_URL")
        if endpoint is None:
            print("CREDENTIAL_PROXY_URL is not configured", file=sys.stderr)
            raise SystemExit(1)
        raw_command = shlex.join([invoked_as, *sys.argv[1:]])
        # Do not consume inherited stdin here. MCP and other stdio-based parent
        # processes may have a protocol stream on fd 0. Pipelines should be sent
        # as one raw command through credential-proxy-exec instead.
        stdin = None
        try:
            kubernetes_context = kubernetes_context_from_env()
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
    else:
        args = parse_args()
        endpoint = args.endpoint
        raw_command = args.command if args.command is not None else sys.stdin.read()
        stdin = None
        try:
            kubernetes_context = kubernetes_context_from_env()
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        if args.context_name:
            if kubernetes_context is None:
                print("--context requires GKE_PROJECT_ID, GKE_LOCATION, and GKE_CLUSTER_NAME", file=sys.stderr)
                raise SystemExit(2)
            kubernetes_context["contextName"] = args.context_name
    if not raw_command:
        print("no command provided", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(
        execute(
            endpoint,
            raw_command,
            stdin=stdin,
            kubernetes_context=kubernetes_context,
        )
    )
