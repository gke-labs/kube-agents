"""Make a typed ``/hermes <subcommand>`` reach the gateway command dispatcher.

Slack only routes a leading-slash string to Hermes' slash handler when that
slash is registered on the Slack app (Features → Slash Commands, or the
manifest ``hermes slack manifest`` prints). When it is not, Slack delivers the
line as an ordinary channel message, the gateway parses ``/hermes`` as the
command name, finds nothing in ``COMMAND_REGISTRY``, and answers "Unknown
command ``/hermes``" — so ``/hermes sethome`` silently does nothing and the
"no home channel is set" prompt keeps reappearing every session.

The Slack adapter already knows how to unwrap the legacy form: its
``_handle_slash_command`` maps ``/hermes <sub> [args]`` through
``slack_subcommand_map()`` before dispatch. This plugin performs the same
unwrapping one layer earlier — on the inbound ``MessageEvent`` — so the legacy
form behaves identically whether it arrived as a registered slash command or as
plain text, on any platform.
"""

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes.plugin.legacy_slash_commands")

# "/hermes", optionally "@botname" (Telegram-style), optionally followed by a
# subcommand and its arguments. Anchored: a mid-sentence mention of /hermes is
# not a command.
_LEGACY_PREFIX_RE = re.compile(r"^/hermes(?:@\S+)?(?:\s+(?P<rest>.*))?$", re.IGNORECASE | re.DOTALL)

# A leading bot mention Slack prepends when the user @-mentions the bot on the
# same line ("<@U123> /hermes sethome").
_LEADING_MENTION_RE = re.compile(r"^<@[UWB][A-Z0-9]+>\s*")


def _subcommand_map() -> Dict[str, str]:
    """Bare subcommand name -> real gateway command (``sethome`` -> ``/sethome``)."""
    from hermes_cli.commands import slack_subcommand_map

    mapping = dict(slack_subcommand_map())
    # The Slack adapter adds this alias after building the map; mirror it so the
    # two paths accept exactly the same vocabulary.
    mapping["compact"] = "/compress"
    return mapping


def rewrite_legacy_hermes_command(text: str) -> Optional[str]:
    """Return the text ``/hermes …`` should dispatch as, or ``None`` to leave it alone.

    Mirrors the Slack adapter's legacy branch:

    - ``/hermes``                 -> ``/help``
    - ``/hermes sethome``         -> ``/sethome``
    - ``/hermes model gpt-5``     -> ``/model gpt-5``
    - ``/hermes what's up?``      -> ``what's up?`` (unknown subcommand is a
      free-form question, not a command — this is why the prefix is stripped
      rather than passed through, which would draw an unknown-command reply)
    """
    if not isinstance(text, str) or not text:
        return None

    stripped = _LEADING_MENTION_RE.sub("", text.strip(), count=1).strip()
    match = _LEGACY_PREFIX_RE.match(stripped)
    if match is None:
        return None

    rest = (match.group("rest") or "").strip()
    if not rest:
        return "/help"

    parts = rest.split(maxsplit=1)
    first_word = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    try:
        mapping = _subcommand_map()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not load the slash subcommand map: %s", exc)
        return None

    target = mapping.get(first_word)
    if target is None:
        # Not a command at all — treat it as the question the user asked.
        return rest

    return f"{target} {args}".strip()


def handle_pre_gateway_dispatch(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Unwrap the legacy ``/hermes`` form before the gateway resolves the command."""
    try:
        original = getattr(event, "text", None)
        rewritten = rewrite_legacy_hermes_command(original)
        if rewritten is None or rewritten == original:
            return None
        logger.info("Rewrote legacy command %r to %r", original, rewritten)
        return {"action": "rewrite", "text": rewritten}
    except Exception as exc:
        logger.error(
            "Error in legacy_slash_commands pre_gateway_dispatch hook: %s",
            exc,
            exc_info=True,
        )
        return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
