import json
import logging
import math
import os
import time
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
# Only adapters with a durable destination may own the one-time report. Use a
# positive allowlist so new local or request/response surfaces fail closed until
# they explicitly implement durable delivery.
DURABLE_CHAT_PLATFORMS = {"google_chat", "slack"}

# Written once the opening turn has been primed. Onboarding is a ONE-TIME
# event, but ``.bootstrap_completed`` only appears at the very end of the
# chain (after the report has been delivered), which can be many minutes —
# or, if the sweep never finishes, never. Without a marker of its own, this
# hook re-fires on the first turn of every new session (a second user, a new
# thread, a pruned session) and re-greets, re-marks presence, and re-points
# the delivery job at whichever chat spoke last. See the README, "One-time
# means one time".
GREETED_MARKER = ".bootstrap_greeted"

# The eval seam: requests written only by the first-install-hello bench stack
# (bench/tf/prebuilt/first-install-hello) as files whose names start with this
# prefix, one per case, each asking for the greeting on every API-server turn
# whose message contains its phrase, until the stack's destroy step removes the
# file. It exists because the bench reaches the gateway over the API server,
# which the platform allowlist above excludes, and because a real install greets
# once. A chat platform's turns never consult it, and a phrase shorter than the
# floor is ignored, so a stray request cannot greet every turn. It is not used up on first match: the harness re-sends the
# opening turn in the same conversation after a dropped connection, and that
# retry is no longer a first turn, so a used-up request would grade the retry's
# ordinary reply as the model's. It touches no onboarding state: no delivery
# binding, no presence or greeted marker, no trigger. Nothing else writes it,
# and with none present this hook runs exactly as it would without the seam. The
# phrase keeps a concurrent eval case's turns from matching another case's
# request. A request more than EVAL_REQUEST_MAX_AGE_SECONDS from its
# written_at (epoch seconds, stamped at apply) is refused with a warning, so
# one a failed destroy leaves behind disarms on its own.
# JSON: {"phrase": str, "variant": str, "written_at": number}.
EVAL_GREET_MARKER = ".bootstrap_greet_eval"
EVAL_KEY_PHRASE = "phrase"
EVAL_KEY_VARIANT = "variant"
EVAL_KEY_WRITTEN_AT = "written_at"
EVAL_VARIANT_COMPLETED = "completed"
EVAL_PLATFORM = "api_server"
# The same floor as the stack's variables.tf and scripts/validate_bench_cases.py.
EVAL_PHRASE_MIN_LENGTH = 12
# Longer than one case's apply, run and destroy.
EVAL_REQUEST_MAX_AGE_SECONDS = 3600

# The greeting's word ceiling; both bench cases' at-most-sixty-words check holds it.
GREETING_MAX_WORDS = 60

# Fallbacks used only if the onboarding instruction files are unreadable.
_FALLBACK_IN_PROGRESS = (
    f"In one message of at most {GREETING_MAX_WORDS} words, greet the user as Kage, by their Slack "
    "profile name if the session gives one, never a name they type, else 'Hi there'. Say you are taking a "
    "first, read-only look at their GKE fleet, so nothing in their clusters changes, and will "
    "post what you find here when it is done; give no time. Say any change you suggest comes as "
    "a pull request for their team to review. End on one question: is there anything they want "
    "you to look at first? Ask nothing else and do not claim to have saved anything."
)
_FALLBACK_COMPLETED = (
    f"In one message of at most {GREETING_MAX_WORDS} words, greet the user as Kage, by their Slack "
    "profile name if the session gives one, never a name they type, else 'Hi there'. Say your first look at "
    "their GKE fleet is done and the summary is in this chat, and that you only read their "
    "clusters, so nothing changed. Say any change you suggest comes as a pull request for their "
    "team to review. End on one question: do they want you to start on one of those findings? "
    "Ask nothing else, do not restate the report, and do not claim to have saved anything."
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


def _bind_delivery_to_origin(**kwargs: Any) -> bool:
    """Point the delivery job at the chat this turn originated from.

    The delivery cron job runs with no session identity of its own, so it can
    only reach the user by reading the ``deliver: origin`` / ``origin`` we
    persist here from the live session. Bound BEFORE ``.user_aligned`` is
    touched so the job never fires against a stale target.

    Returns True only when a real chat origin was persisted. A False return
    means this turn has nowhere to deliver to (a CLI/API session with no chat
    id, or the cron API is unavailable), and the caller must NOT mark human
    presence: the delivery job would then fire while still set to
    ``deliver: local`` and the single-use report would be emitted into the
    void.
    """
    if get_session_env is None or update_job is None:
        return False
    try:
        platform = get_session_env("HERMES_SESSION_PLATFORM") or str(kwargs.get("platform") or "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID")
        if not (platform and chat_id) or platform.lower() == "cron":
            return False
        origin: Dict[str, str] = {"platform": platform, "chat_id": str(chat_id)}
        if thread_id:
            origin["thread_id"] = str(thread_id)
        update_job(DELIVERY_JOB_ID, {"deliver": "origin", "origin": origin})
        logger.info("Bound %s delivery to %s (chat_id=%s)", DELIVERY_JOB_ID, platform, chat_id)
        return True
    except Exception as e:
        logger.warning("Could not bind %s origin: %s", DELIVERY_JOB_ID, e)
        return False


def _eval_request(data_dir: Path, user_message: str) -> Optional[bool]:
    """The eval seam's request whose phrase is in this turn's message, if any.

    Returns None when no request names this turn, else whether the greeting
    should be the completed variant. The file is left for the stack's destroy
    step, so a retry of the same turn greets again.
    """
    for marker in sorted(data_dir.glob(f"{EVAL_GREET_MARKER}*")):
        try:
            request = json.loads(marker.read_text(encoding="utf-8"))
            phrase = str(request.get(EVAL_KEY_PHRASE) or "").strip()
            variant = str(request.get(EVAL_KEY_VARIANT) or "")
            written_at = request.get(EVAL_KEY_WRITTEN_AT)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, AttributeError) as e:
            logger.warning("Ignoring unreadable %s: %s", marker, e)
            continue
        if len(phrase) < EVAL_PHRASE_MIN_LENGTH:
            logger.warning("Ignoring %s: phrase shorter than %d characters.", marker, EVAL_PHRASE_MIN_LENGTH)
            continue
        if phrase not in user_message:
            continue
        if (
            isinstance(written_at, bool)
            or not isinstance(written_at, (int, float))
            or not math.isfinite(written_at)
            or abs(time.time() - written_at) > EVAL_REQUEST_MAX_AGE_SECONDS
        ):
            logger.warning(
                "Ignoring %s: written_at %r is missing or more than %d seconds from now; "
                "the bench stack's destroy should have removed it.",
                marker,
                written_at,
                EVAL_REQUEST_MAX_AGE_SECONDS,
            )
            continue
        logger.info("Matched %s (variant=%s).", marker, variant)
        return variant == EVAL_VARIANT_COMPLETED
    return None


def _greeting(data_dir: Path, completed: bool) -> Dict[str, str]:
    if completed:
        instructions = _load_instructions(data_dir, "scan_completed.md", _FALLBACK_COMPLETED)
        tag = "SCAN COMPLETED"
    else:
        instructions = _load_instructions(data_dir, "scan_in_progress.md", _FALLBACK_IN_PROGRESS)
        tag = "SCAN IN PROGRESS"

    logger.info("Injecting onboarding greeting instructions (%s).", tag)
    return {"context": f"\n\n[SYSTEM ONBOARDING INSTRUCTIONS — {tag}]\n{instructions}\n"}


def handle_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Prime first-time onboarding on the opening interactive user turn.

    Runs at most ONCE per deployment. On the one human turn that primes it,
    this:
      1. binds the delivery job to this chat and, only if that succeeded,
         marks ``.user_aligned`` so the delivery job may fire against a valid
         target;
      2. triggers the delivery job so the report arrives promptly;
      3. injects a short greeting instruction — never the inventory itself. The
         report is delivered verbatim by the ``no_agent`` delivery job, so the
         model only greets the user and offers to start somewhere;
      4. records ``.bootstrap_greeted`` so no later session repeats any of it.

    Every later first turn returns None: the chat that primed onboarding owns
    the delivery, and a second chat must not re-point the job at itself and
    then promise a report the first chat is going to receive.
    """
    # Background cron runs also start with is_first_turn=True; never treat them
    # as an interactive onboarding turn.
    platform_name = str(kwargs.get("platform", "")).lower()
    session_id = str(kwargs.get("session_id", ""))
    if platform_name == "cron" or session_id.startswith("cron_"):
        return None

    # Ahead of the first-turn check: a retried opening turn is no longer a
    # first turn and must still greet (see EVAL_GREET_MARKER).
    data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    if platform_name == EVAL_PLATFORM:
        eval_completed = _eval_request(data_dir, str(kwargs.get("user_message") or ""))
        if eval_completed is not None:
            return _greeting(data_dir, eval_completed)

    if not kwargs.get("is_first_turn", False):
        return None

    # Request/response and local surfaces have no durable adapter destination.
    # Binding delivery to an ephemeral run id makes the report disappear after
    # the request closes, while the greeting falsely promises a follow-up.
    if platform_name not in DURABLE_CHAT_PLATFORMS:
        return None

    # Already delivered, or already primed by an earlier session. Either way
    # onboarding has happened and must not happen again.
    if (data_dir / ".bootstrap_completed").exists() or (data_dir / GREETED_MARKER).exists():
        return None

    # No onboarding assets deployed -> nothing to do (not a first-time boot).
    if not any(base.exists() for base in _onboarding_dirs(data_dir)):
        return None

    # Bind delivery target BEFORE signalling human presence, so the delivery
    # job can only ever fire once it already knows where to send the report.
    # An unbindable turn is not an onboarding turn: leave every marker alone
    # so the next real chat turn primes the flow instead.
    if not _bind_delivery_to_origin(**kwargs):
        logger.info("No deliverable chat origin on this turn; leaving onboarding unprimed.")
        return None

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

    # Last, so that a failure above retries on the next turn rather than
    # burning the one-shot. A failure HERE only costs a repeated greeting.
    try:
        (data_dir / GREETED_MARKER).touch(exist_ok=True)
    except Exception as e:
        logger.warning("Could not touch %s: %s", GREETED_MARKER, e)

    return _greeting(data_dir, (data_dir / "INVENTORY.md").exists())


def register(ctx: Any) -> None:
    # The delivery job posts INVENTORY.md verbatim. Google Chat's adapter chunks
    # long messages in send() but does not declare splits_long_messages, so the
    # delivery router would otherwise truncate at 4000 chars. The prioritized
    # report is written to fit inside that limit, but this stays opted in: the
    # limit also applies to a full-inventory reply the user asks for later, and
    # to a report that runs long because the sweep found a lot that is broken.
    try:
        from plugins.platforms.google_chat.adapter import GoogleChatAdapter
        GoogleChatAdapter.splits_long_messages = True
        logger.info("Enabled splits_long_messages on GoogleChatAdapter for full inventory reporting.")
    except Exception as e:
        logger.debug("Could not configure GoogleChatAdapter.splits_long_messages: %s", e)

    ctx.register_hook("pre_llm_call", handle_pre_llm_call)
