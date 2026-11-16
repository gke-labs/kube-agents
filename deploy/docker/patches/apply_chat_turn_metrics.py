#!/usr/bin/env python3
"""Wire gateway/chat_turn_metrics.py into the three places a synchronous turn fails.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree, after
``chat_turn_metrics.py`` has been copied to ``gateway/`` and after
``apply_slack_boilerplate.py``, whose comment in the turn runner the third
anchor follows. Two files, three sites, one import each:

``gateway/run_turn.py``: ``_hmwa_agent_error_reply``, the ``except`` body of
the agent turn, counts the turn before it does anything else, so an exception
out of the turn counts once under the turn's platform; and the delivery of a
result the agent marked ``failed`` (a provider 429 or 5xx, a connection error,
the gateway timeout: Hermes returns those as a failed result) counts it
before ``_hmwa_deliver_turn_response`` sends it.

``gateway/run_turn_runner.py``: the provider-authentication failure, whose
reply ``apply_slack_boilerplate.py`` already rewrites, counts before the reply
is built.

Each site calls ``_kage_chat_turn_metrics.record_turn_error(source)``, which
never raises and counts nothing when the ``chat_metrics`` plugin is absent, so
the gateway behaves as upstream ships it. Anchors are derived against
v2026.9.14.

Usage::

    python3 apply_chat_turn_metrics.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

PREFIX = "chat-turn-metrics"
MARKER = "kube-agents patch: chat-turn-metrics"
DEFAULT_HERMES_ROOT = "/opt/hermes"

RUN_TURN = "gateway/run_turn.py"
RUN_TURN_RUNNER = "gateway/run_turn_runner.py"

IMPORT_ANCHOR = "\nimport logging\n"
IMPORT_PATCHED = (
    "\nimport logging\n"
    f"from gateway import chat_turn_metrics as _kage_chat_turn_metrics  # noqa: E402  {MARKER}\n"
)

# The first statement of _hmwa_agent_error_reply's body, after its docstring.
ERROR_REPLY_ANCHOR = (
    "        # Retain Slack thread/workspace routing so a failed turn cannot leave its status visible.\n"
)
ERROR_REPLY_PATCHED = (
    f"        _kage_chat_turn_metrics.record_turn_error(source)  # {MARKER}\n"
    + ERROR_REPLY_ANCHOR
)

# The delivery of the turn's result, in the try body whose except is the
# error reply above; `source` and `agent_result` are both in scope here.
DELIVER_ANCHOR = "            return await self._hmwa_deliver_turn_response(\n"
DELIVER_PATCHED = (
    f"            if isinstance(agent_result, dict) and agent_result.get(\"failed\"):  # {MARKER}\n"
    "                _kage_chat_turn_metrics.record_turn_error(source)\n"
    + DELIVER_ANCHOR
)

# apply_slack_boilerplate.py's comment at the provider-authentication failure;
# the count goes right after it, before the reply is built.
AUTH_ANCHOR = (
    "            # kube-agents patch: KAGE_SLACK_UX logs the exception and keeps it out of\n"
    "            # a Slack reply; see gateway/slack_boilerplate.py.\n"
)
AUTH_PATCHED = AUTH_ANCHOR + f"            _kage_chat_turn_metrics.record_turn_error(ctx.source)  # {MARKER}\n"


def apply(root: Path) -> None:
    run_turn = patchlib.Patch(root, RUN_TURN, prefix=PREFIX)
    run_turn.refuse_if_patched(MARKER)
    run_turn.substitute(IMPORT_ANCHOR, IMPORT_PATCHED, label="run_turn import")
    run_turn.substitute(ERROR_REPLY_ANCHOR, ERROR_REPLY_PATCHED, label="error reply count")
    run_turn.substitute(DELIVER_ANCHOR, DELIVER_PATCHED, label="failed result count")
    run_turn.commit("3 anchors, 1 import")

    runner = patchlib.Patch(root, RUN_TURN_RUNNER, prefix=PREFIX)
    runner.refuse_if_patched(MARKER)
    runner.substitute(IMPORT_ANCHOR, IMPORT_PATCHED, label="run_turn_runner import")
    runner.substitute(AUTH_ANCHOR, AUTH_PATCHED, label="provider authentication count")
    runner.commit("1 anchor, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT))
