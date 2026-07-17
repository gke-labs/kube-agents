import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from gateway.session_context import get_session_env
    from cron.jobs import update_job
except ImportError:
    get_session_env = None  # type: ignore[assignment]
    update_job = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _perform_onboarding_cleanup(data_dir: Path) -> None:
    """Deterministically executes final onboarding self-cleanup right inside Python.

    Creates `.bootstrap_completed`, unlinks the single-use `INVENTORY.md` marker from
    disk, right alongside removing both the background discovery routine and dedicated
    delivery notification job out of active cron configurations.
    """
    completed_marker = data_dir / ".bootstrap_completed"
    try:
        completed_marker.touch(exist_ok=True)
        logger.info("Created durable state marker (.bootstrap_completed) to conclude onboarding.")
    except Exception as e:
        logger.warning("Could not touch %s: %s", completed_marker, e)

    inventory_path = data_dir / "INVENTORY.md"
    if inventory_path.exists():
        try:
            inventory_path.unlink()
            logger.info("Removed %s straight from workspace volume.", inventory_path.name)
        except Exception as e:
            logger.warning("Could not remove %s: %s", inventory_path, e)

    hermes_bin = "/opt/hermes/.venv/bin/hermes"
    if not Path(hermes_bin).exists():
        hermes_bin = "hermes"

    for job_id in ("bootstrap-inventory-scan", "bootstrap-inventory-delivery"):
        try:
            subprocess.run(
                [str(hermes_bin), "cron", "rm", job_id],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except Exception:
            pass

def handle_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Intercepts conversation turns right before language model execution during GKE boot.

    On opening interactive turns (`is_first_turn=True`), Python assesses explicit state markers,
    touches `.user_aligned`, dynamically binds `bootstrap-inventory-delivery` right across to the
    active chat room, and serves branchless instructions directly into prompt context.
    """
    if not kwargs.get("is_first_turn", False):
        return None

    # Exclude background scheduled cron routines from receiving interactive onboarding injections
    platform_name = str(kwargs.get("platform", "")).lower()
    session_id = str(kwargs.get("session_id", ""))
    if platform_name == "cron" or session_id.startswith("cron_"):
        return None

    data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    if (data_dir / ".bootstrap_completed").exists():
        return None

    onboarding_data_dir = data_dir / "onboarding"
    onboarding_defaults_dir = Path("/opt/defaults/onboarding")
    if not onboarding_data_dir.exists() and not onboarding_defaults_dir.exists():
        return None

    # Touch deterministic alignment marker across initial user contact
    try:
        (data_dir / ".user_aligned").touch(exist_ok=True)
        logger.info("Created %s coordination marker right across initial interaction.", data_dir / ".user_aligned")
    except Exception as e:
        logger.warning("Notice: could not touch .user_aligned marker inside hook: %s", e)

    # Dynamically point our dedicated background delivery notification job straight to THIS origin chat space
    try:
        if get_session_env is not None and update_job is not None:
            current_platform = get_session_env("HERMES_SESSION_PLATFORM") or str(kwargs.get("platform") or "")
            current_chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
            current_thread_id = get_session_env("HERMES_SESSION_THREAD_ID")

            if current_platform and current_chat_id and current_platform.lower() != "cron":
                origin_data = {
                    "platform": current_platform,
                    "chat_id": str(current_chat_id),
                }
                if current_thread_id:
                    origin_data["thread_id"] = str(current_thread_id)

                update_job("bootstrap-inventory-delivery", {
                    "deliver": "origin",
                    "origin": origin_data,
                })
                logger.info(
                    "Dynamically bound bootstrap-inventory-delivery target directly to room: %s (chat_id=%s)",
                    current_platform,
                    current_chat_id,
                )
    except Exception as e:
        logger.warning("Could not update bootstrap-inventory-delivery origin configuration: %s", e)

    inventory_file = data_dir / "INVENTORY.md"
    if inventory_file.exists():
        # Case B: Scan already completed across quiet boot. Inject master findings right alongside immediate self-cleanup.
        scan_completed_path = onboarding_data_dir / "scan_completed.md"
        if not scan_completed_path.exists():
            scan_completed_path = onboarding_defaults_dir / "scan_completed.md"

        try:
            instructions = scan_completed_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Could not read scan_completed.md: %s", e)
            instructions = "Present completed GKE technical discovery findings right inside chat window."

        try:
            inventory_content = inventory_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Could not read INVENTORY.md during pre_llm_call: %s", e)
            inventory_content = "[Error reading /opt/data/INVENTORY.md.]"

        logger.info("Injecting completed environment inventory directly inside Turn 1 prompt.")
        context = (
            f"\n\n[SYSTEM ONBOARDING INSTRUCTIONS — SCAN COMPLETED]\n{instructions}\n\n"
            f"--- EXCLUSIVE COMPLETED ENVIRONMENT INVENTORY FINDINGS ---\n{inventory_content}\n--- END FINDINGS ---\n"
        )

        # Execute onboarding cleanup immediately across Python in Case B upon reading findings into context memory
        _perform_onboarding_cleanup(data_dir)
        return {"context": context}
    else:
        # Case A: Discovery scan running across the background right now right alongside active chat contact
        scan_in_progress_path = onboarding_data_dir / "scan_in_progress.md"
        if not scan_in_progress_path.exists():
            scan_in_progress_path = onboarding_defaults_dir / "scan_in_progress.md"

        try:
            instructions = scan_in_progress_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Could not read scan_in_progress.md: %s", e)
            instructions = "Explain that background environment discovery (bootstrap-inventory-scan) is actively running across the cluster right now."

        logger.info("Injecting active discovery roadmap instructions across Turn 1.")
        context = f"\n\n[SYSTEM ONBOARDING INSTRUCTIONS — SCAN IN PROGRESS]\n{instructions}\n"
        return {"context": context}


def handle_post_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Evaluates background cron completion turns right right right right right to execute deterministic onboarding cleanup across Python.

    When `bootstrap-inventory-delivery` successfully formats and returns the completed GKE inventory summary
    across chat (`Case A`), Python reads `assistant_response` passed from `turn_finalizer.py` right alongside triggering
    immediate onboarding self-cleanup right after text generation right before subsequent cron evaluations.
    """
    session_id = str(kwargs.get("session_id", ""))
    if "bootstrap-inventory-delivery" not in session_id:
        return None

    response_text = str(kwargs.get("assistant_response") or kwargs.get("response") or "").strip()
    if not response_text or response_text.upper() == "[SILENT]" or "[SILENT]" in response_text.upper():
        return None

    logger.info("bootstrap-inventory-delivery produced complete inventory transmission text — executing immediately onboarding self-cleanup right across post_llm_call.")
    data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    _perform_onboarding_cleanup(data_dir)
    return None


def register(ctx: Any) -> None:
    # Runtime patch right for GoogleChatAdapter right to enable complete multi-chunk delivery completely without gateway truncation
    try:
        from plugins.platforms.google_chat.adapter import GoogleChatAdapter
        GoogleChatAdapter.splits_long_messages = True
        logger.info("Enabled splits_long_messages on GoogleChatAdapter for full technical inventory reporting.")
    except Exception as e:
        logger.debug("Notice: could not configure GoogleChatAdapter.splits_long_messages right across hook registration: %s", e)

    ctx.register_hook("pre_llm_call", handle_pre_llm_call)
    ctx.register_hook("post_llm_call", handle_post_llm_call)
