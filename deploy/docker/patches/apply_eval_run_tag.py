#!/usr/bin/env python3
"""Record an API turn's eval run tag for its session.

Two anchored insertions and a trailer in
``gateway/platforms/api_server_openai_routes.py``. In ``_handle_chat_completions``
and ``_handle_responses``, once the turn's ``session_id`` is settled and before
anything runs the turn, one line awaits
``hermes_cli.eval_run_tag.record_request(request, session_id)``, which records
``X-Kube-Agents-Eval-Run`` for the session in the Session KV server while
``KUBEAGENTS_EVAL_RUN_TAGS`` is on and returns at once, the header unread,
while it is off. The trailer imports it, and falls back to a no-op coroutine
if the import fails, so a broken module can never take the API server down.

Why is documented in ``deploy/docker/patches/eval_run_tag.py``. Usage::

    python3 apply_eval_run_tag.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

PREFIX = "eval_run_tag"
ROUTES_RELATIVE = "gateway/platforms/api_server_openai_routes.py"
MARKER = "kube-agents patch: eval_run_tag"
RECORD = "_kube_agents_eval_run_record"
ROUTES_CLASS = "OpenAICompatRoutesMixin"
CALL = f"        await {RECORD}(request, session_id)  # {MARKER}\n"

#: The chat-completions handler: a fresh session id is derived, then the
#: completion id is minted. The call goes between them, after the
#: ``X-Hermes-Session-Id`` branch has settled ``session_id`` too.
CHAT_ANCHOR = (
    "            session_id = _derive_chat_session_id(system_prompt, first_user)\n"
    '        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"\n'
)
CHAT_REPLACEMENT = CHAT_ANCHOR.replace("        completion_id", CALL + "        completion_id", 1)

#: The responses handler: the session precedence expression, then ``stream``.
RESPONSES_ANCHOR = (
    "            or self._declared_conversation_session(gateway_session_key)\n"
    "            or str(uuid.uuid4()))\n"
    '        stream = _coerce_request_bool(body.get("stream"), default=False)\n'
)
RESPONSES_REPLACEMENT = RESPONSES_ANCHOR.replace("        stream =", CALL + "        stream =", 1)

TRAILER = (
    f"\n\n# {MARKER}: record an API turn's X-Kube-Agents-Eval-Run for its session\n"
    "# while KUBEAGENTS_EVAL_RUN_TAGS is on. See hermes_cli/eval_run_tag.py.\n"
    "try:\n"
    f"    from hermes_cli.eval_run_tag import record_request as {RECORD}\n"
    "except Exception:  # noqa: BLE001 - never fail the API server over an eval tag\n"
    f"    async def {RECORD}(request, session_id):\n"
    "        return None\n"
)

#: Each handler and the anchor that must sit inside it.
SITES = (
    ("_handle_chat_completions", CHAT_ANCHOR, CHAT_REPLACEMENT),
    ("_handle_responses", RESPONSES_ANCHOR, RESPONSES_REPLACEMENT),
)


def apply(root: Path) -> None:
    """Apply both insertions and the trailer under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, ROUTES_RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(MARKER)
    for function, anchor, _ in SITES:
        site = patch.find_method(ROUTES_CLASS, function, label="API handler")
        inside = patch.source[site.start : site.end].count(anchor)
        if inside != 1:
            raise SystemExit(
                f"{PREFIX} patch: {ROUTES_RELATIVE}: expected the session anchor once inside "
                f"{function}(), found {inside}. {patch.note}"
            )
    for function, anchor, replacement in SITES:
        patch.substitute(anchor, replacement, label=f"{function} session id")
    patch.append(TRAILER)
    try:
        compile(patch.source, patch.relative, "exec")
    except SyntaxError as exc:
        raise SystemExit(f"{PREFIX} patch: {ROUTES_RELATIVE} no longer parses after patching: {exc}")
    patch.commit("an API turn records its eval run tag for its session")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
