"""Read Google's ``RetryInfo.retryDelay`` out of a 429 that has no ``Retry-After``.

The gap
-------
``agent/conversation_loop.py`` (Hermes v2026.8.19) honours exactly one source of
"how long until the quota opens": the ``Retry-After`` response header, capped at
600 s. Everything else waits ``jittered_backoff(base_delay=2.0)``, which puts
the default three retries roughly 2 to 3 s and 4 to 6 s apart.

Google does not send that header. A Gemini or Vertex 429 carries its reset
window in the error *body*, as ``google.rpc.RetryInfo``::

    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
               "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "54s"}]}}

LiteLLM passes that body through as the message of its own 429, and the OpenAI
SDK the worker uses puts the whole thing in ``str(error)``::

    Error code: 429 - {'error': {'message': 'litellm.RateLimitError:
    VertexAIException - {"error": {..., "retryDelay": "54s"}}', ...}}

So during a quota storm a worker burns all three retries inside ten seconds of a
window the provider said would be closed for a minute, then exits. This module
is the other half of the fix in ``kanban_guardrail_exit.py``: that one makes the
exit legible on the board, this one makes the retries land where they can
succeed.

What it does
------------
:func:`retry_delay_from_error` looks for ``retryDelay`` in the error's text, its
``message``, its ``body`` and its response JSON, following the cause chain the
same five levels ``agent/error_classifier.py`` does, and returns the delay in
seconds capped at :data:`RETRY_DELAY_CAP_SECONDS` (the header path's cap) or
``None``. The loop calls it only when the header said nothing, so a provider
that sends both keeps the header's answer. Nothing here changes the retry count.
"""

from __future__ import annotations

import json
import re

#: ``retryDelay`` as ``google.rpc.RetryInfo`` spells it in JSON, tolerant of the
#: quoting it arrives in: JSON (``"retryDelay": "54s"``), the Python repr the
#: OpenAI SDK prints (``'retryDelay': '54s'``), and the backslash-escaped form
#: of either when the body has been serialised inside another string. The value
#: is a protobuf Duration, always seconds with an ``s`` suffix, fractional or not.
RETRY_DELAY_PATTERN = re.compile(
    r"""retryDelay\\*["']?\s*:\s*\\*["']?\s*(\d+(?:\.\d+)?)\s*s\b"""
)

#: Same cap as the ``Retry-After`` header path in ``conversation_loop.py``:
#: 600 s covers every realistic provider reset window and rejects pathological
#: values (upstream #26293).
RETRY_DELAY_CAP_SECONDS = 600.0

#: How far down ``__cause__`` / ``__context__`` to look for the body. Matches
#: ``_extract_error_body`` in ``agent/error_classifier.py``.
CAUSE_CHAIN_DEPTH = 5


def retry_delay_from_text(text, *, cap=RETRY_DELAY_CAP_SECONDS):
    """The first ``retryDelay`` in ``text`` as seconds, capped, or ``None``.

    A zero or negative delay is ``None`` too: it is not a wait, and returning
    it would make the caller skip the default backoff and retry immediately.
    """
    if not text:
        return None
    match = RETRY_DELAY_PATTERN.search(str(text))
    if match is None:
        return None
    try:
        seconds = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(seconds, float(cap))


def _texts_of(error):
    """Every string the delay could be hiding in, most direct first."""
    yield str(error)
    message = getattr(error, "message", None)
    if isinstance(message, str):
        yield message
    body = getattr(error, "body", None)
    if isinstance(body, (dict, list)):
        try:
            yield json.dumps(body, default=str)
        except Exception:
            yield str(body)
    elif body is not None:
        yield str(body)
    response = getattr(error, "response", None)
    if response is not None:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, (dict, list)):
            try:
                yield json.dumps(payload, default=str)
            except Exception:
                yield str(payload)


def retry_delay_from_error(error, *, cap=RETRY_DELAY_CAP_SECONDS):
    """The provider's ``retryDelay`` for this exception, in seconds, or ``None``.

    Walks the exception and its cause chain, reading ``str()``, ``message``,
    ``body`` and ``response.json()`` at each level. Never raises: the caller is
    inside the retry loop's error handler and a broken parser must fall back to
    the stock backoff rather than kill the worker.
    """
    current = error
    seen = set()
    for _ in range(CAUSE_CHAIN_DEPTH):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        try:
            for text in _texts_of(current):
                delay = retry_delay_from_text(text, cap=cap)
                if delay is not None:
                    return delay
        except Exception:
            pass
        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
    return None
