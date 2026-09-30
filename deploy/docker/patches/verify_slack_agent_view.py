#!/usr/bin/env python3
"""Build-time behaviour gate for the Slack agent-view patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
right after ``apply_slack_agent_view.py``.
Drives the real ``slack_manifest_command`` and the adapter's real
``_assistant_suggested_prompts``, once with ``KAGE_SLACK_UX`` unset and once
with it on, and asserts on what they return:

* flag off: the default manifest is upstream's ``assistant`` experience, and an
  unset ``suggested_prompts`` yields none;
* flag on: the default and ``--no-assistant`` manifests equal the flag-off ones
  (agent view is one-way in Slack, so only ``--agent-view`` picks it);
  ``--agent-view --name`` describes the app by that name and does not subscribe
  ``agent_session_stopped``, which would offer a Stop nothing handles; and an
  unset ``suggested_prompts`` yields the three kube-agents prompts while a
  configured one still wins;
* either way: the emitted bot scopes include ``reactions:write``,
  ``users:read`` and ``files:write``, the scopes the Slack UX work spends.

``test_slack_agent_view.py`` covers the applier against fixtures on the host;
this is the only check that reaches the tree that ships.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from apply_slack_agent_view import FLAG_ENV, SUGGESTED_PROMPTS

MANIFEST_MODULE = "hermes_cli.slack_cli"
ADAPTER_MODULE = "plugins.platforms.slack.adapter"

REQUIRED_SCOPES = ("reactions:write", "users:read", "files:write")
FLAG_ON = "true"
APP_NAME = "Kage"
AGENT_DESCRIPTION = f"Chat with {APP_NAME} in Slack Messages."
STOP_EVENT = "agent_session_stopped"

CONFIGURED_PROMPTS = [{"title": "configured", "message": "configured"}]


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_agent_view verify: {detail}")


def _import(root: Path, name: str):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module(name)


@contextlib.contextmanager
def _flag(value: str | None):
    saved = os.environ.pop(FLAG_ENV, None)
    if value is not None:
        os.environ[FLAG_ENV] = value
    try:
        yield
    finally:
        os.environ.pop(FLAG_ENV, None)
        if saved is not None:
            os.environ[FLAG_ENV] = saved


def _manifest(module, **flags) -> dict:
    args = argparse.Namespace(**flags)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = module.slack_manifest_command(args)
    if code != 0:
        raise _fail(f"hermes slack manifest {flags} exited {code}")
    return json.loads(out.getvalue())


def _prompts(cls, extra: dict) -> list:
    adapter = SimpleNamespace(config=SimpleNamespace(extra=extra))
    return cls._assistant_suggested_prompts(adapter)[1]


def _check_scopes(manifest: dict, label: str) -> None:
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    missing = [scope for scope in REQUIRED_SCOPES if scope not in scopes]
    if missing:
        raise _fail(f"{label} manifest lacks bot scopes {missing}: {scopes}")


def main(root: Path = Path("/opt/hermes")) -> None:
    manifest_module = _import(root, MANIFEST_MODULE)
    adapter_cls = _import(root, ADAPTER_MODULE).SlackAdapter

    with _flag(None):
        off = _manifest(manifest_module)
        if "agent_view" in off["features"] or "assistant_view" not in off["features"]:
            raise _fail(f"flag off, default features are {sorted(off['features'])}")
        _check_scopes(off, "flag-off")
        bare_off = _manifest(manifest_module, no_assistant=True)
        if _prompts(adapter_cls, {}):
            raise _fail("flag off, an unset suggested_prompts yields prompts")

    with _flag(FLAG_ON):
        if _manifest(manifest_module) != off:
            raise _fail("flag on, the default manifest differs from flag off's")
        if _manifest(manifest_module, no_assistant=True) != bare_off:
            raise _fail("flag on, the --no-assistant manifest differs from flag off's")
        agent = _manifest(manifest_module, agent_view=True, name=APP_NAME)
        description = agent["features"].get("agent_view", {}).get("agent_description")
        if description != AGENT_DESCRIPTION:
            raise _fail(f"flag on, --agent-view --name {APP_NAME} describes the app as {description!r}")
        if STOP_EVENT in agent["settings"]["event_subscriptions"]["bot_events"]:
            raise _fail(f"flag on, --agent-view subscribes {STOP_EVENT}")
        _check_scopes(agent, "flag-on agent-view")
        got = [row["message"] for row in _prompts(adapter_cls, {})]
        if got != list(SUGGESTED_PROMPTS):
            raise _fail(f"flag on, default prompts are {got}")
        configured = _prompts(adapter_cls, {"suggested_prompts": CONFIGURED_PROMPTS})
        if configured != CONFIGURED_PROMPTS:
            raise _fail(f"flag on, a configured suggested_prompts became {configured}")

    print(
        f"slack_agent_view verify: flag off is upstream's; flag on changes only --agent-view's "
        f"description, with {len(SUGGESTED_PROMPTS)} default prompts"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
