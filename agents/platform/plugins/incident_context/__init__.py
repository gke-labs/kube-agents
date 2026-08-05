import json
import urllib.request
from urllib.parse import urlencode

def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_inbound)

def on_inbound(*, event, **_):
    src = event.source
    platform = getattr(src.platform, "value", str(src.platform))
    import logging
    logger = logging.getLogger(__name__)
    logger.info("platform=%s, chat_id=%s, thread_id=%s", platform, getattr(src, 'chat_id', None), getattr(src, 'thread_id', None))
    if platform not in ("google_chat", "slack") or not src.thread_id:
        return None
    # A slash command is addressed to the gateway, not to the incident. Both
    # this hook and `legacy_slash_commands` are `pre_gateway_dispatch`, and
    # whichever rewrites first decides what the other one sees: prepending the
    # triage report moves `/hermes sethome` off the front of the line, so the
    # unwrap never matches and the gateway reads the whole thing as prose. The
    # user gets a paragraph of last week's incident instead of their command,
    # inside the one thread where they are most likely to be running one.
    if (getattr(event, "text", "") or "").lstrip().startswith("/"):
        return None
    report = _lookup(src.chat_id, src.thread_id)
    if not report:
        return None  # not an incident thread -> leave the message untouched
    new_text = (
        "[Prior k8s incident report posted in this thread - use it to interpret the reply below]\n"
        f"{report}\n\n"
        f"[User reply in thread]: {event.text}"
    )
    return {"action": "rewrite", "text": new_text}

def _lookup(chat_id, thread_id):
    q = urlencode({"chat_id": chat_id, "thread_id": thread_id})
    url = f"http://127.0.0.1:8699/v1/incidents/by-thread?{q}"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            if r.status == 200:
                return json.load(r).get("report")
    except Exception:
        pass  # fail-open: never break normal message flow
    return None
