#!/usr/bin/env python3
"""Replay the #3206 fixture against an install's inference gateway and count imitations.

usage: python3 replay.py <fixture.json> --arm main|patched [--runs 3] [--gateway URL] [--model ALIAS]

`main` sends the fixture as built, the history Hermes v2026.9.14 sends today. `patched`
first runs every message through `replace_unstructured_reasoning` from
deploy/docker/patches/unstructured_reasoning.py, the call the agent image's patch inserts
into Hermes's message builders, so the leaked reply goes out as the bare marker.

Each run is one chat/completions call. A run is red when the reply's content opens with a
`thought` line, the same rule the patch and the Hermes bridge use. The gateway defaults to a
local port-forward of svc/inference-gateway. Prints one line per run and a summary; exits 1
when the arm's result is not the expected one (main: every run red; patched: none).
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.request
from pathlib import Path

CHAT_PATH = "/v1/chat/completions"
# The in-cluster gateway takes any bearer; the header only satisfies the client.
AUTH_HEADER = "Bearer none"
TIMEOUT_S = 300
DEFAULT_GATEWAY = "http://127.0.0.1:8080"  # a port-forward of svc/inference-gateway
DEFAULT_MODEL = "model-default"
DEFAULT_RUNS = 3

PATCHES = Path(__file__).resolve().parents[3] / "deploy" / "docker" / "patches"
sys.path.insert(0, str(PATCHES))
import unstructured_reasoning  # noqa: E402  (the rule the image patch installs)


def request_body(fixture: dict, arm: str, model: str) -> dict:
    body = copy.deepcopy(fixture)
    body["model"] = model
    if arm == "patched":
        for message in body["messages"]:
            unstructured_reasoning.replace_unstructured_reasoning(message)
    return body


def call(gateway: str, body: dict) -> dict:
    req = urllib.request.Request(
        gateway.rstrip("/") + CHAT_PATH,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": AUTH_HEADER},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--arm", choices=("main", "patched"), required=True)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)

    body = request_body(json.loads(args.fixture.read_text()), args.arm, args.model)
    red = 0
    for run in range(1, args.runs + 1):
        message = call(args.gateway, body)["choices"][0]["message"]
        content = message.get("content") or ""
        imitated = unstructured_reasoning.opens_with_unstructured_reasoning(content)
        red += imitated
        print(json.dumps({"arm": args.arm, "run": run, "opens_with_thought": imitated,
                          "content_chars": len(content), "tool_calls": len(message.get("tool_calls") or [])}))
    print(f"{args.arm}: {red} of {args.runs} replies opened with a thought line")
    expected = args.runs if args.arm == "main" else 0
    return 0 if red == expected else 1


if __name__ == "__main__":
    sys.exit(main())
