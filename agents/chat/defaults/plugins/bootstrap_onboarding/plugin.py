import importlib.util
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional, Tuple

try:
    from gateway.session_context import get_session_env
    from cron.jobs import update_job, trigger_job
except ImportError:
    get_session_env = None  # type: ignore[assignment]
    update_job = None  # type: ignore[assignment]
    trigger_job = None  # type: ignore[assignment]

# Apart from the imports above, so a Hermes without these names still binds the
# delivery job and greets.
try:
    from gateway.config import HomeChannel, Platform, load_gateway_config, persist_home_channel
    from hermes_cli.config import save_env_value
except ImportError:
    HomeChannel = None  # type: ignore[assignment,misc]
    Platform = None  # type: ignore[assignment,misc]
    load_gateway_config = None  # type: ignore[assignment]
    persist_home_channel = None  # type: ignore[assignment]
    save_env_value = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# The jobs that can post the report: oobe's delivery stage (agents/chat/scripts/oobe.py), and the
# old delivery job, which still delivers on an install whose oobe removed itself before this image,
# and again after a rollback to an image from before the fold. Each one present is bound.
DELIVERY_JOB_IDS = ("oobe", "bootstrap-inventory-delivery")
# Only adapters with a durable destination may own the one-time report. Use a
# positive allowlist so new local or request/response surfaces fail closed until
# they explicitly implement durable delivery.
DURABLE_CHAT_PLATFORMS = {"google_chat", "slack"}
# Under spec.mode next every chat turn reaches Hermes through the API server,
# relayed by the hermes-bridge, so the session names no chat. Such a turn is a
# chat turn only when the bridge recorded a durable platform's conversation for
# its session (_prime_conversation); any other API-server turn (the bench, curl,
# the inject and A2A doors) has no such route and still fails closed.
API_SERVER_PLATFORM = "api_server"
# The legacy home-channel env var /sethome mirrors. Hermes names both durable
# platforms' by this pattern; the thread id's is the same name plus a suffix.
HOME_CHANNEL_ENV_FORMAT = "{}_HOME_CHANNEL"
HOME_THREAD_ENV_SUFFIX = "_THREAD_ID"
SESSION_PLATFORM_ENV = "HERMES_SESSION_PLATFORM"
SESSION_CHAT_ID_ENV = "HERMES_SESSION_CHAT_ID"
SESSION_CHAT_NAME_ENV = "HERMES_SESSION_CHAT_NAME"
SESSION_USER_ID_ENV = "HERMES_SESSION_USER_ID"
SESSION_SCOPE_ID_ENV = "HERMES_SESSION_SCOPE_ID"

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
# JSON: {"phrase": str, "variant": str, "written_at": number,
# "home_channel_set": bool (optional; true adds HOME_CHANNEL_INSTRUCTION),
# "report": str (required with from_report)}.
# The variant names the greeting, or is EVAL_VARIANT_FROM_REPORT: pick it with
# the opening turn's report check, run on the file the request's "report" names
# rather than the real report, so a case never puts a file where onboarding
# reads one.
EVAL_GREET_MARKER = ".bootstrap_greet_eval"
EVAL_KEY_PHRASE = "phrase"
EVAL_KEY_VARIANT = "variant"
EVAL_KEY_WRITTEN_AT = "written_at"
# True tells the plugin to greet as if this turn made the chat the home channel.
EVAL_KEY_HOME_CHANNEL_SET = "home_channel_set"
EVAL_VARIANT_COMPLETED = "completed"
EVAL_VARIANT_FROM_REPORT = "from_report"
EVAL_KEY_REPORT = "report"
# A bare file name beside the real report that nothing in onboarding reads.
EVAL_REPORT_NAME = re.compile(r"\AINVENTORY\.bench-[a-z0-9-]+\.md\Z")
EVAL_PLATFORM = API_SERVER_PLATFORM
# The same floor as the stack's variables.tf and scripts/validate_bench_cases.py.
EVAL_PHRASE_MIN_LENGTH = 12
# Longer than one case's apply, run and destroy.
EVAL_REQUEST_MAX_AGE_SECONDS = 3600

# The ranked report, where bootstrap_delivery.py reads it. With the shell
# sandbox on it is on the sandbox pod's data volume, which this pod does not
# mount, so its presence is read across with sandbox_exec, as delivery does.
REPORT_NAME = "INVENTORY.md"
SANDBOX_HOME = "/opt/data"
# The image's copy, not $HERMES_HOME/scripts on the volume.
SANDBOX_EXEC_PATH = Path("/opt/defaults/scripts/sandbox_exec.py")
SANDBOX_EXEC_MODULE = "_kube_agents_sandbox_exec"
# The opening turn waits on this read. A slower sandbox greets as "in progress",
# which stays true: delivery posts the report once it can read it.
REPORT_CHECK_TIMEOUT_SECONDS = 5

# The image's chat.notify helper, which reads the bridge's conversation route
# and knows which platform's conversations the gateway serves.
CHAT_NOTIFY_PATH = Path("/opt/defaults/scripts/chat_notify.py")
CHAT_NOTIFY_MODULE = "_kube_agents_chat_notify"
# The image's hand-off module, whose read_scan_marker is the one reader of the
# sweep marker's filing time that oobe and the hand-off share.
BOOTSTRAP_HANDOFF_PATH = Path("/opt/defaults/scripts/bootstrap_handoff.py")
BOOTSTRAP_HANDOFF_MODULE = "_kube_agents_bootstrap_handoff"
# The conversation the report goes to under next, read by bootstrap_delivery.py
# (CONVERSATION_FILE there) and kept after delivery as onboarding's destination.
# The jobs stay ``deliver: local``: the scheduler cannot post to a gateway
# conversation, and a job origin naming one would print the report to nobody.
CONVERSATION_FILE = "onboarding_conversation.json"
# The conversation path's "a human has connected", in place of .user_aligned,
# which delivery reads as "post to the job's origin". An image from before this
# path never reads it, so a rollback leaves the report waiting, not printed.
CONVERSATION_ALIGNED_MARKER = ".user_aligned_conversation"
# Taken with O_EXCL for the length of one priming: each top-level message is
# its own first turn under next, so two quick messages could otherwise both
# greet and bind. One older than PRIMING_STALE_SECONDS is a priming that died.
PRIMING_MARKER = ".bootstrap_priming"
PRIMING_STALE_SECONDS = 120
# Written by bootstrap_scan_gate.py when it files the sweep. A sweep older than
# SCAN_MAX_AGE_SECONDS belongs to an install that ran before this path existed:
# its first new thread would otherwise get a weeks-old report as onboarding.
SCAN_FILED_MARKER = ".bootstrap_scan_filed"
SCAN_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
CONVERSATION_JOB_ID = "oobe"
# Delivery's record for the bound conversation: its count of attempts that did
# not post, and the outcome of the post that did (bootstrap_delivery.py,
# CONVERSATION_ATTEMPTS_FILE and CONVERSATION_DONE_FILE). A new binding starts
# both again, so a re-run is judged on its own attempts.
CONVERSATION_ATTEMPTS_FILE = ".bootstrap_conversation_attempts.json"
CONVERSATION_DONE_FILE = ".bootstrap_conversation_done"
# The hold that keeps the report behind the greeting: an empty file named by the
# opening turn's id, created before the aligned marker and removed by
# post_llm_call when the greeting is on its way, when this turn also marks the
# delivery job due. Delivery (bootstrap_delivery.py, GREETING_WAIT_SECONDS)
# stays silent while one is young, so the greeting posts first instead of
# racing the next cron tick. Aged by mtime there, so it carries no content.
GREETING_PENDING_PREFIX = ".bootstrap_greeting_pending."
# The Chat Agent's home when HERMES_HOME is unset, as the delivery stage reads it.
DEFAULT_DATA_DIR = "/opt/data"
# The turns this process holds delivery for, so post_llm_call on any other turn
# touches no disk. A gateway restart empties it and leaves the file to age out.
_greeting_turns: set = set()

# The greeting's word ceiling; both bench cases' at-most-sixty-words check holds it.
GREETING_MAX_WORDS = 60

# Added to the greeting instructions only when this turn made the chat the home
# channel. The sentence names no command, because the command to move the home
# channel is different on each platform.
HOME_CHANNEL_INSTRUCTION = (
    "This turn made this chat the home channel, so scheduled reports also come here. "
    "After the point about where the results appear, add one short sentence that says this, "
    "for example: \"Scheduled reports come here too.\" "
    "Name no command. Stay inside the word limit."
)

# Fallbacks used only if the onboarding instruction files are unreadable.
_FALLBACK_IN_PROGRESS = (
    f"In one message of at most {GREETING_MAX_WORDS} words, greet the user as kube-agents, by their Slack "
    "profile name if the session gives one, never a name they type, else 'Hi there'. Say you are taking a "
    "first, read-only look at their GKE fleet, so nothing in their clusters changes, and will "
    "post what you find here when it is done; give no time. Say any change you suggest comes as "
    "a pull request for their team to review. End on one question: is there anything they want "
    "you to look at first? Ask nothing else and do not claim to have saved anything."
)
_FALLBACK_COMPLETED = (
    f"In one message of at most {GREETING_MAX_WORDS} words, greet the user as kube-agents, by their Slack "
    "profile name if the session gives one, never a name they type, else 'Hi there'. Say your first look at "
    "their GKE fleet is done and the summary follows in this chat, and that you only read their "
    "clusters, so nothing changed. Say any change you suggest comes as a pull request for their "
    "team to review. End on one question: do they want you to start on one of those findings? "
    "Ask nothing else, do not restate the report, and do not claim to have saved anything."
)


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", DEFAULT_DATA_DIR))


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


def _bind_delivery_to_origin(**kwargs: Any) -> list:
    """Point the delivery job at the chat this turn originated from.

    The delivery cron job runs with no session identity of its own, so it can
    only reach the user by reading the ``deliver: origin`` / ``origin`` we
    persist here from the live session. Bound BEFORE ``.user_aligned`` is
    touched so the job never fires against a stale target.

    Returns the jobs a real chat origin was persisted on. An empty return
    means this turn has nowhere to deliver to (a CLI/API session with no chat
    id, or the cron API is unavailable), and the caller must NOT mark human
    presence: the delivery job would then fire while still set to
    ``deliver: local`` and the single-use report would be emitted into the
    void.
    """
    if get_session_env is None or update_job is None:
        return []
    try:
        platform = get_session_env("HERMES_SESSION_PLATFORM") or str(kwargs.get("platform") or "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID")
        if not (platform and chat_id) or platform.lower() == "cron":
            return []
        origin: Dict[str, str] = {"platform": platform, "chat_id": str(chat_id)}
        if thread_id:
            origin["thread_id"] = str(thread_id)
    except Exception as e:
        logger.warning("Could not read the chat origin: %s", e)
        return []
    bound = []
    for job_id in DELIVERY_JOB_IDS:
        try:
            # update_job answers None for a job not in the store.
            if update_job(job_id, {"deliver": "origin", "origin": origin}) is not None:
                bound.append(job_id)
                logger.info("Bound %s delivery to %s (chat_id=%s)", job_id, platform, chat_id)
        except Exception as e:
            logger.warning("Could not bind %s origin: %s", job_id, e)
    return bound


def _set_home_channel_if_unset(**kwargs: Any) -> bool:
    """Make this chat the platform's home channel when none is configured.

    Returns True only when this call wrote the home channel.

    ``deliver: chat`` jobs and the chat relay post to the home channel, and a
    fresh install has none until someone runs ``/sethome``, so every scheduled
    report is dropped until then. A configured channel is left alone, whether
    ``/sethome`` or the CR's ``homeChannel`` (a home-channel env var) set it.
    The write goes through ``persist_home_channel``, as ``/sethome``'s does: a
    ``home_channel`` block without ``platform`` or ``chat_id`` raises out of
    ``load_gateway_config`` and stops cron delivery for every platform.
    """
    if None in (get_session_env, HomeChannel, Platform, load_gateway_config, persist_home_channel):
        return False
    try:
        platform = Platform(get_session_env(SESSION_PLATFORM_ENV) or str(kwargs.get("platform") or ""))
        chat_id = str(get_session_env(SESSION_CHAT_ID_ENV) or "")
        if not chat_id or load_gateway_config().get_home_channel(platform) is not None:
            return False
        # No thread_id: a home channel pinned to the opening message's thread would
        # put every scheduled report into that one thread.
        persist_home_channel(
            HomeChannel(
                platform=platform,
                chat_id=chat_id,
                name=get_session_env(SESSION_CHAT_NAME_ENV) or chat_id,
                user_id=get_session_env(SESSION_USER_ID_ENV) or None,
                scope_id=get_session_env(SESSION_SCOPE_ID_ENV) or None,
            )
        )
        logger.info("Set the %s home channel to %s.", platform.value, chat_id)
    except Exception as e:
        logger.warning("Could not set the home channel: %s", e)
        return False
    # The legacy env mirror /sethome also writes, for consumers that read it.
    if save_env_value is None:
        return True
    env_var = HOME_CHANNEL_ENV_FORMAT.format(platform.value.upper())
    try:
        save_env_value(env_var, chat_id)
        save_env_value(env_var + HOME_THREAD_ENV_SUFFIX, "")
    except Exception as e:
        logger.warning("Home channel saved but %s was not: %s", env_var, e)
    return True


def _eval_request(data_dir: Path, user_message: str) -> Optional[Tuple[bool, bool]]:
    """The eval seam's request whose phrase is in this turn's message, if any.

    Returns None when no request names this turn. Else it returns two values:
    whether to give the completed greeting, and whether to greet as if this
    turn set the home channel. The file stays for the stack's destroy step, so
    a retry of the same turn greets again.
    """
    for marker in sorted(data_dir.glob(f"{EVAL_GREET_MARKER}*")):
        try:
            request = json.loads(marker.read_text(encoding="utf-8"))
            phrase = str(request.get(EVAL_KEY_PHRASE) or "").strip()
            variant = str(request.get(EVAL_KEY_VARIANT) or "")
            written_at = request.get(EVAL_KEY_WRITTEN_AT)
            home_channel_set = request.get(EVAL_KEY_HOME_CHANNEL_SET) is True
            report_name = str(request.get(EVAL_KEY_REPORT) or "")
        except FileNotFoundError:
            continue
        except (OSError, ValueError, AttributeError, RecursionError) as e:
            logger.warning("Ignoring unreadable %s: %s", marker, e)
            continue
        if len(phrase) < EVAL_PHRASE_MIN_LENGTH:
            logger.warning("Ignoring %s: phrase shorter than %d characters.", marker, EVAL_PHRASE_MIN_LENGTH)
            continue
        if phrase not in user_message:
            continue
        try:
            age = abs(time.time() - float(written_at))
        except (TypeError, ValueError, OverflowError):
            age = math.inf
        if (
            isinstance(written_at, bool)
            or not isinstance(written_at, (int, float))
            or not math.isfinite(age)
            or age > EVAL_REQUEST_MAX_AGE_SECONDS
        ):
            logger.warning(
                "Ignoring %s: written_at %r is missing or more than %d seconds from now; "
                "the bench stack's destroy should have removed it.",
                marker,
                written_at,
                EVAL_REQUEST_MAX_AGE_SECONDS,
            )
            continue
        if variant == EVAL_VARIANT_FROM_REPORT and not EVAL_REPORT_NAME.match(report_name):
            logger.warning("Ignoring %s: %s names no report file matching %s.", marker, EVAL_KEY_REPORT, EVAL_REPORT_NAME.pattern)
            continue
        logger.info("Matched %s (variant=%s, home_channel_set=%s).", marker, variant, home_channel_set)
        if variant == EVAL_VARIANT_FROM_REPORT:
            return _report_ready(data_dir, report_name), home_channel_set
        return variant == EVAL_VARIANT_COMPLETED, home_channel_set
    return None


def _load_image_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Loaded once: until onboarding greets, every API-server turn reads the route.
_chat_notify: Optional[ModuleType] = None
# Platforms already warned about as unserved, so a mismatch logs once.
_unserved_warned: set = set()


def _load_chat_notify() -> ModuleType:
    global _chat_notify
    if _chat_notify is None:
        _chat_notify = _load_image_module(CHAT_NOTIFY_PATH, CHAT_NOTIFY_MODULE)
    return _chat_notify


def _load_sandbox_exec() -> ModuleType:
    return _load_image_module(SANDBOX_EXEC_PATH, SANDBOX_EXEC_MODULE)


def _report_ready(data_dir: Path, name: str = REPORT_NAME) -> bool:
    """Whether the ranked report, or the eval seam's ``name``, is written where
    delivery reads the report.

    Never raises. A sandbox read that fails or outlasts
    REPORT_CHECK_TIMEOUT_SECONDS is False, and so is anything that keeps this
    from telling whether the sandbox is on, after a look at the local path.
    """
    try:
        sandbox_exec = _load_sandbox_exec()
        in_sandbox = sandbox_exec.sandbox_enabled()
    except Exception as e:
        logger.warning("Could not tell whether the shell sandbox is on; checking %s locally: %s", name, e)
        in_sandbox = False
    if not in_sandbox:
        try:
            return (data_dir / name).exists()
        except OSError:
            return False
    try:
        report = sandbox_exec.read_bytes(
            f"{SANDBOX_HOME}/{name}", max_bytes=1, timeout=REPORT_CHECK_TIMEOUT_SECONDS
        )
    except Exception as e:
        logger.warning("Could not read %s in the shell sandbox; greeting as in progress: %s", name, e)
        return False
    return report is not None


def _scan_too_old(data_dir: Path) -> bool:
    """Whether the sweep was filed more than SCAN_MAX_AGE_SECONDS ago, by the clock oobe reads.

    A marker that cannot be read, or a module that cannot be loaded, is not too old.
    """
    try:
        handoff = _load_image_module(BOOTSTRAP_HANDOFF_PATH, BOOTSTRAP_HANDOFF_MODULE)
        filed = handoff.read_scan_marker(data_dir / SCAN_FILED_MARKER)
    except Exception as e:
        logger.warning("Could not read %s: %s", SCAN_FILED_MARKER, e)
        return False
    return filed is not None and time.time() - filed[1] > SCAN_MAX_AGE_SECONDS


def _take_priming(data_dir: Path) -> bool:
    """Take the priming marker; False when a live priming holds it or it cannot be taken."""
    marker = data_dir / PRIMING_MARKER
    for _ in range(2):
        try:
            os.close(os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return True
        except FileExistsError:
            try:
                if time.time() - marker.stat().st_mtime < PRIMING_STALE_SECONDS:
                    return False
                marker.unlink()
                logger.warning("Removed a stale %s.", PRIMING_MARKER)
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning("Could not clear a stale %s: %s", PRIMING_MARKER, e)
                return False
        except OSError as e:
            logger.warning("Could not take %s: %s", PRIMING_MARKER, e)
            return False
    return False


def _write_conversation(data_dir: Path, route: Dict[str, str]) -> None:
    target = data_dir / CONVERSATION_FILE
    tmp = target.with_name(target.name + ".tmp")
    record = {
        "platform": route["platform"],
        "conversation": route["conversation"],
        "context_id": route["context_id"],
        "bound_at": time.time(),
    }
    tmp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)


def _hold_delivery(data_dir: Path, turn_id: str) -> bool:
    """Hold the report behind this turn's greeting. False when it could not be
    held, and the caller marks the delivery job due at once, as before the hold."""
    if not turn_id:
        return False
    try:
        (data_dir / f"{GREETING_PENDING_PREFIX}{turn_id}").touch()
    except OSError as e:
        logger.warning("Could not hold delivery behind the greeting: %s", e)
        return False
    _greeting_turns.add(turn_id)
    return True


def _drop_hold(data_dir: Path, turn_id: str) -> None:
    _greeting_turns.discard(turn_id)
    try:
        (data_dir / f"{GREETING_PENDING_PREFIX}{turn_id}").unlink()
    except FileNotFoundError:
        pass  # delivery aged it out already
    except OSError as e:
        logger.warning("Could not remove the greeting hold for turn %s: %s", turn_id, e)


def _trigger_first(*job_ids: str) -> None:
    """Mark the first of ``job_ids`` the store has due. Only the first: trigger_job
    also enables a job, and the old delivery entry ships disabled beside oobe."""
    if trigger_job is None:
        return
    for job_id in job_ids:
        try:
            # None is a job the store does not have; nothing was enabled.
            if trigger_job(job_id) is not None:
                return
        except Exception as e:
            logger.warning("Could not trigger %s: %s", job_id, e)
            return


def _prime_conversation(data_dir: Path, session_id: str, turn_id: str = "") -> Optional[Dict[str, str]]:
    """Prime onboarding on an API-server first turn the hermes-bridge relayed from a chat.

    The report goes to the bridge's recorded conversation through the gateway
    (bootstrap_delivery.py, ``a2a notify --conversation``), so nothing here
    touches a cron job's origin, ``.user_aligned`` or a Hermes home channel,
    which under next drives nothing. Returns None, with every marker left as
    it was, for a turn with no durable conversation or a gateway that does not
    serve its platform's conversations.
    """
    try:
        chat_notify = _load_chat_notify()
        route = chat_notify.conversation_route(session_id)
        served = chat_notify.gateway_platform()
    except Exception as e:
        logger.warning("Could not read the conversation route for %s: %s", session_id, e)
        return None
    if not route or route["platform"] not in DURABLE_CHAT_PLATFORMS:
        return None
    if served != route["platform"]:
        if route["platform"] in _unserved_warned:
            return None
        _unserved_warned.add(route["platform"])
        logger.warning(
            "The gateway does not serve %s conversations here (%s=%r); leaving onboarding unprimed.",
            route["platform"], chat_notify.NOTIFY_CONVERSATIONS_ENV, served,
        )
        return None
    if _scan_too_old(data_dir):
        logger.info("The onboarding sweep is older than %d seconds; not priming onboarding.", SCAN_MAX_AGE_SECONDS)
        return None
    if not _take_priming(data_dir):
        return None
    try:
        # Again under the priming marker: a turn that passed the first check
        # while another was priming must not bind its own chat after it.
        if (data_dir / ".bootstrap_completed").exists() or (data_dir / GREETED_MARKER).exists():
            return None
        try:
            (data_dir / CONVERSATION_ATTEMPTS_FILE).unlink(missing_ok=True)
            (data_dir / CONVERSATION_DONE_FILE).unlink(missing_ok=True)
            _write_conversation(data_dir, route)
        except Exception as e:
            logger.warning("Could not record the onboarding conversation: %s", e)
            return None
        logger.info("Bound onboarding delivery to %s conversation %s.", route["platform"], route["conversation"])

        # Before the marker and the trigger, as on the chat-platform path.
        report_ready = _report_ready(data_dir)

        held = _hold_delivery(data_dir, turn_id)
        try:
            (data_dir / CONVERSATION_ALIGNED_MARKER).touch(exist_ok=True)
        except Exception as e:
            logger.warning("Could not touch %s: %s", CONVERSATION_ALIGNED_MARKER, e)
            # Delivery reads the file alone as a binding; leave none behind.
            _drop_hold(data_dir, turn_id)
            try:
                (data_dir / CONVERSATION_FILE).unlink(missing_ok=True)
            except OSError as e2:
                logger.warning("Could not remove %s: %s", CONVERSATION_FILE, e2)
            return None
        if not held:
            _trigger_first(CONVERSATION_JOB_ID)
        try:
            (data_dir / GREETED_MARKER).touch(exist_ok=True)
        except Exception as e:
            logger.warning("Could not touch %s: %s", GREETED_MARKER, e)
        return _greeting(data_dir, report_ready)
    finally:
        try:
            (data_dir / PRIMING_MARKER).unlink()
        except OSError:
            pass


def _greeting(data_dir: Path, completed: bool, home_channel_set: bool = False) -> Dict[str, str]:
    if completed:
        instructions = _load_instructions(data_dir, "scan_completed.md", _FALLBACK_COMPLETED)
        tag = "SCAN COMPLETED"
    else:
        instructions = _load_instructions(data_dir, "scan_in_progress.md", _FALLBACK_IN_PROGRESS)
        tag = "SCAN IN PROGRESS"

    if home_channel_set:
        instructions = f"{instructions}\n\n{HOME_CHANNEL_INSTRUCTION}"
    logger.info("Injecting onboarding greeting instructions (%s, home_channel_set=%s).", tag, home_channel_set)
    return {"context": f"\n\n[SYSTEM ONBOARDING INSTRUCTIONS — {tag}]\n{instructions}\n"}


def handle_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Prime first-time onboarding on the opening interactive user turn.

    Runs at most ONCE per deployment, the eval seam below aside: an API-server
    turn naming a live eval request is greeted every time. Under spec.mode
    next a chat turn arrives as an API-server turn and is primed by
    _prime_conversation instead of the steps below. On the one human turn that
    primes it on a chat platform, this:
      1. binds the delivery job to this chat and, only if that succeeded,
         marks ``.user_aligned`` so the delivery job may fire against a valid
         target, and makes this chat the home channel if none is configured;
      2. holds the delivery job behind this turn's greeting (``post_llm_call``
         marks it due once the greeting is on its way);
      3. injects a short greeting instruction — never the inventory itself. The
         report is delivered by the ``no_agent`` delivery job, verbatim, or
         laid out for Slack when ``KAGE_SLACK_UX`` is on and the job delivers
         to Slack, so the model only greets the user and offers to start
         somewhere;
      4. records ``.bootstrap_greeted`` so no later session repeats any of it.

    Every later first turn returns None: the chat that primed onboarding owns
    the delivery, and a second chat must not re-point the job at itself and
    then promise a report the first chat is going to receive.
    """
    # Background cron runs also start with is_first_turn=True; never treat them
    # as an interactive onboarding turn.
    platform_name = str(kwargs.get("platform", "")).lower()
    session_id = str(kwargs.get("session_id", ""))
    # Stable for the turn, unlike the session id, which can rotate on compression.
    turn_id = str(kwargs.get("turn_id") or "")
    if platform_name == "cron" or session_id.startswith("cron_"):
        return None

    # Ahead of the first-turn check: a retried opening turn is no longer a
    # first turn and must still greet (see EVAL_GREET_MARKER).
    data_dir = _data_dir()
    if platform_name == EVAL_PLATFORM:
        eval_request = _eval_request(data_dir, str(kwargs.get("user_message") or ""))
        if eval_request is not None:
            return _greeting(data_dir, *eval_request)

    # A bridged conversation keeps one session, so if its route could not be
    # read (or the bridge could not record it) on the first message, a later
    # message is the only retry: an API-server turn is checked on every turn
    # until onboarding has greeted.
    if not kwargs.get("is_first_turn", False) and platform_name != API_SERVER_PLATFORM:
        return None

    # Request/response and local surfaces have no durable adapter destination.
    # Binding delivery to an ephemeral run id makes the report disappear after
    # the request closes, while the greeting falsely promises a follow-up. An
    # API-server turn goes on only to _prime_conversation's route check.
    if platform_name not in DURABLE_CHAT_PLATFORMS and platform_name != API_SERVER_PLATFORM:
        return None

    # Already delivered, or already primed by an earlier session. Either way
    # onboarding has happened and must not happen again.
    if (data_dir / ".bootstrap_completed").exists() or (data_dir / GREETED_MARKER).exists():
        return None

    # No onboarding assets deployed -> nothing to do (not a first-time boot).
    if not any(base.exists() for base in _onboarding_dirs(data_dir)):
        return None

    if platform_name == API_SERVER_PLATFORM:
        return _prime_conversation(data_dir, session_id, turn_id)

    # Bind delivery target BEFORE signalling human presence, so the delivery
    # job can only ever fire once it already knows where to send the report.
    # An unbindable turn is not an onboarding turn: leave every marker alone
    # so the next real chat turn primes the flow instead.
    bound = _bind_delivery_to_origin(**kwargs)
    if not bound:
        logger.info("No deliverable chat origin on this turn; leaving onboarding unprimed.")
        return None

    home_channel_set = _set_home_channel_if_unset(**kwargs)

    # Before .user_aligned and the trigger: once either is in place, delivery
    # can claim the report and move it aside before this read lands.
    report_ready = _report_ready(data_dir)

    held = _hold_delivery(data_dir, turn_id)
    try:
        (data_dir / ".user_aligned").touch(exist_ok=True)
        logger.info("Marked %s (human connected).", data_dir / ".user_aligned")
    except Exception as e:
        logger.warning("Could not touch .user_aligned: %s", e)

    if not held:
        _trigger_first(bound[0])

    # Last, so that a failure above retries on the next turn rather than
    # burning the one-shot. A failure HERE only costs a repeated greeting.
    try:
        (data_dir / GREETED_MARKER).touch(exist_ok=True)
    except Exception as e:
        logger.warning("Could not touch %s: %s", GREETED_MARKER, e)

    return _greeting(data_dir, report_ready, home_channel_set)


def handle_post_llm_call(**kwargs: Any) -> None:
    """Release the report once the opening turn's greeting is on its way.

    Hermes fires this once per turn, after the model's last call and before the
    reply leaves the agent, so the delivery job marked due here runs on a tick
    after the greeting posts. A turn this process did not hold delivery for
    returns before any disk access. A greeting turn that ends with no text
    fires no hook: delivery drops the hold itself after GREETING_WAIT_SECONDS.
    """
    turn_id = str(kwargs.get("turn_id") or "")
    if turn_id not in _greeting_turns:
        return None
    data_dir = _data_dir()
    _drop_hold(data_dir, turn_id)
    _trigger_first(*DELIVERY_JOB_IDS)
    return None


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
    ctx.register_hook("post_llm_call", handle_post_llm_call)
