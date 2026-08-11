"""Translate a Responses-style payload into a contract event stream.

This is not M4.1. Making Hermes pass the conformance suite is a separate
milestone with a live runner behind it. What this module does is keep the
contract honest at design time: the event shapes claim to be modelled on what
`/v1/responses` already emits, and a translator that runs over a recorded
payload is the difference between that being true and it being asserted.

It is also where the legacy sniffing goes to be quarantined. A Responses
``function_call_output`` carries no status, so ``bench/kube_agents_bench/parsing.py``
has to infer failure from the output text. The contract's ``tool_result`` has an
explicit ``status`` precisely so nothing downstream repeats that guess -- and the
guess has to live somewhere until the producer is fixed, so it lives here, at the
boundary, with a name that says what it is.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

import schema

_TOOL_ERROR_PREFIX = "Error executing tool"

# The producer replaces a re-emitted output with this notice when an identical
# one appears later in the same payload. Emitting it as a tool_result would
# publish the placeholder as though it were the tool's answer.
_ELIDED_OUTPUT = re.compile(r"^\[Duplicate tool output\b.*\]$", re.DOTALL)


def _flatten_output(output: Any) -> str:
    """Collapse a ``function_call_output.output`` to text.

    Streaming and non-streaming builders wrap the same payload differently, so
    both the bare string and the list-of-blocks shape are accepted.
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, list):
        parts = []
        for block in output:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return json.dumps(output, default=str)


def _flatten_message(item: dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "output_text"
        and isinstance(block.get("text"), str)
    )


def _decode_arguments(raw: Any) -> dict[str, Any]:
    """``function_call.arguments`` is a JSON *string* on the wire."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    if isinstance(raw, dict):
        return raw
    return {} if raw is None else {"raw": raw}


def sniff_failure(text: str) -> bool:
    """Guess whether a status-less tool output represents a failure.

    Deliberately conservative: free text is never scanned for the word "error",
    because tool output legitimately contains it (a grep hit, a test log). Only
    the structured shapes count.

    Named ``sniff_`` so no reader mistakes it for a fact the producer reported.
    A conforming runner sets ``status`` and never calls this.
    """
    if text.startswith(_TOOL_ERROR_PREFIX):
        return True
    try:
        data = json.loads(text.strip())
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("success") is False or data.get("ok") is False:
        return True
    exit_code = data.get("exit_code", data.get("returncode"))
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    # An error reported beside a real payload is a diagnostic, not a failure.
    return bool(data.get("error")) and not (
        data.get("content") or data.get("result") or data.get("structuredContent")
    )


def events_from_responses(
    payload: dict[str, Any],
    *,
    run_id: str,
    profile: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield contract events for one Responses payload.

    Three things the translation has to handle, all of them consequences of the
    endpoint being stateful and replaying earlier turns:

    - A ``function_call`` repeating a ``call_id`` already seen is a replay, not
      a second call, and is dropped -- emitting it would break the contract's
      uniqueness requirement and read downstream as a tool loop.
    - A ``function_call_output`` whose ``call_id`` matches no call in this
      payload cannot be attributed, and the contract forbids an unattributable
      ``tool_result``. It is dropped rather than orphaned.
    - An elided duplicate output is dropped, leaving its call unanswered, which
      is the truth: this payload does not carry that result.
    """
    seq = 0

    def emit(body: dict[str, Any]) -> dict[str, Any]:
        nonlocal seq
        event = {
            "contract_version": schema.CONTRACT_VERSION,
            "run_id": run_id,
            "seq": seq,
            **body,
        }
        seq += 1
        return event

    started: dict[str, Any] = {"type": schema.RUN_STARTED}
    if profile is not None:
        started["profile"] = profile
    yield emit(started)

    items = payload.get("output")
    items = items if isinstance(items, list) else []
    seen_calls: set[str] = set()
    answered: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")

        if kind == "message" and item.get("role") == "assistant":
            text = _flatten_message(item)
            if text:
                yield emit({"type": schema.MESSAGE, "role": "assistant", "text": text})

        elif kind == "function_call":
            call_id = item.get("call_id")
            if not call_id:
                # The contract requires a correlatable id and the payload has
                # none to offer, so this call cannot be represented.
                continue
            call_id = str(call_id)
            if call_id in seen_calls:
                continue
            seen_calls.add(call_id)
            yield emit(
                {
                    "type": schema.TOOL_CALL,
                    "call_id": call_id,
                    "name": str(item.get("name") or "unknown"),
                    "arguments": _decode_arguments(item.get("arguments")),
                }
            )

        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not call_id:
                continue
            call_id = str(call_id)
            if call_id not in seen_calls or call_id in answered:
                continue
            text = _flatten_output(item.get("output"))
            if _ELIDED_OUTPUT.match(text.strip()):
                continue
            answered.add(call_id)
            yield emit(
                {
                    "type": schema.TOOL_RESULT,
                    "call_id": call_id,
                    "status": "error" if sniff_failure(text) else "completed",
                    "output": text,
                }
            )

    finished: dict[str, Any] = {
        "type": schema.RUN_FINISHED,
        "status": "completed" if payload.get("status") in (None, "completed") else "failed",
    }
    if finished["status"] == "failed":
        finished["error"] = f"upstream response status {payload.get('status')!r}"
    usage = payload.get("usage")
    if isinstance(usage, dict):
        buckets = {
            key: usage[key]
            for key in ("input_tokens", "output_tokens", "total_tokens")
            if isinstance(usage.get(key), int)
        }
        if buckets:
            finished["usage"] = buckets
    yield emit(finished)
