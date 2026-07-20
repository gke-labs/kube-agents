import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from gateway.session_context import get_session_env
    from cron.jobs import update_job, trigger_job
except ImportError:
    get_session_env = None  # type: ignore[assignment]
    update_job = None  # type: ignore[assignment]
    trigger_job = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DELIVERY_JOB_ID = "bootstrap-inventory-delivery"

# Fallbacks used only if the onboarding instruction files are unreadable.
_FALLBACK_IN_PROGRESS = (
    "Greet the user as their Platform Agent. Explain that background GKE "
    "environment discovery is running and its full report will be delivered to "
    "this chat as soon as it finishes. Ask for the team's SOPs and time zone."
)
_FALLBACK_COMPLETED = (
    "Greet the user as their Platform Agent. Explain that GKE environment "
    "discovery is complete and its full report is being delivered to this chat "
    "now. Ask for the team's SOPs and time zone."
)


def _onboarding_dirs(data_dir: Path) -> list[Path]:
    return [data_dir / "onboarding", Path("/opt/defaults/onboarding")]


def _load_instructions(data_dir: Path, name: str, fallback: str) -> str:
    for base in _onboarding_dirs(data_dir):
        path = base / name
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Could not read %s: %s", path, e)
    return fallback


def _bind_delivery_to_origin(**kwargs: Any) -> None:
    """Point the delivery job at the chat this turn originated from.

    The delivery cron job runs with no session identity of its own, so it can
    only reach the user by reading the ``deliver: origin`` / ``origin`` we
    persist here from the live session. Bound BEFORE ``.user_aligned`` is
    touched so the job never fires against a stale target.
    """
    if get_session_env is None or update_job is None:
        return
    try:
        platform = get_session_env("HERMES_SESSION_PLATFORM") or str(kwargs.get("platform") or "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID")
        if not (platform and chat_id) or platform.lower() == "cron":
            return
        origin: Dict[str, str] = {"platform": platform, "chat_id": str(chat_id)}
        if thread_id:
            origin["thread_id"] = str(thread_id)
        update_job(DELIVERY_JOB_ID, {"deliver": "origin", "origin": origin})
        logger.info("Bound %s delivery to %s (chat_id=%s)", DELIVERY_JOB_ID, platform, chat_id)
    except Exception as e:
        logger.warning("Could not bind %s origin: %s", DELIVERY_JOB_ID, e)


def handle_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Prime first-time onboarding on the opening interactive user turn.

    On the first human turn (and only a human turn), this:
      1. binds the delivery job to this chat, then marks ``.user_aligned`` so
         the delivery job may fire against a valid target;
      2. triggers the delivery job so the report arrives promptly;
      3. injects a short greeting instruction — never the inventory itself. The
         report is delivered verbatim by the ``no_agent`` delivery job, so the
         model only greets the user and asks for their operating preferences.
    """
    if not kwargs.get("is_first_turn", False):
        return None

    # Background cron runs also start with is_first_turn=True; never treat them
    # as an interactive onboarding turn.
    platform_name = str(kwargs.get("platform", "")).lower()
    session_id = str(kwargs.get("session_id", ""))
    if platform_name == "cron" or session_id.startswith("cron_"):
        return None

    data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    if (data_dir / ".bootstrap_completed").exists():
        return None

    # No onboarding assets deployed -> nothing to do (not a first-time boot).
    if not any(base.exists() for base in _onboarding_dirs(data_dir)):
        return None

    # Bind delivery target BEFORE signalling human presence, so the delivery
    # job can only ever fire once it already knows where to send the report.
    _bind_delivery_to_origin(**kwargs)

    try:
        (data_dir / ".user_aligned").touch(exist_ok=True)
        logger.info("Marked %s (human connected).", data_dir / ".user_aligned")
    except Exception as e:
        logger.warning("Could not touch .user_aligned: %s", e)

    if trigger_job is not None:
        try:
            trigger_job(DELIVERY_JOB_ID)
        except Exception as e:
            logger.warning("Could not trigger %s: %s", DELIVERY_JOB_ID, e)

    if (data_dir / "INVENTORY.md").exists():
        instructions = _load_instructions(data_dir, "scan_completed.md", _FALLBACK_COMPLETED)
        tag = "SCAN COMPLETED"
    else:
        instructions = _load_instructions(data_dir, "scan_in_progress.md", _FALLBACK_IN_PROGRESS)
        tag = "SCAN IN PROGRESS"

    logger.info("Injecting onboarding greeting instructions (%s).", tag)
    return {"context": f"\n\n[SYSTEM ONBOARDING INSTRUCTIONS — {tag}]\n{instructions}\n"}


def register(ctx: Any) -> None:
    # The delivery job posts the full, verbose INVENTORY.md verbatim. Google
    # Chat's adapter chunks long messages in send() but does not declare
    # splits_long_messages, so the delivery router would otherwise truncate the
    # report at 4000 chars. Opt the adapter in so full reports are delivered.
    try:
        from plugins.platforms.google_chat.adapter import GoogleChatAdapter
        GoogleChatAdapter.splits_long_messages = True
        logger.info("Enabled splits_long_messages on GoogleChatAdapter for full inventory reporting.")
    except Exception as e:
        logger.debug("Could not configure GoogleChatAdapter.splits_long_messages: %s", e)

    ctx.register_hook("pre_llm_call", handle_pre_llm_call)
