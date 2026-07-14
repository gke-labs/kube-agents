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


def execute(
    endpoint: str,
    command: str,
    stdin: str | None = None,
) -> int:
    request_payload = {"requestId": str(uuid.uuid4()), "command": command}
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
    else:
        args = parse_args()
        endpoint = args.endpoint
        raw_command = args.command if args.command is not None else sys.stdin.read()
        stdin = None
    if not raw_command:
        print("no command provided", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(
        execute(
            endpoint,
            raw_command,
            stdin=stdin,
        )
    )
