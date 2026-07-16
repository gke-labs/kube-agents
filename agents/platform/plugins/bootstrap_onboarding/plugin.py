import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from gateway.session_context import get_session_env
    from cron.jobs import update_job
except ImportError:
    get_session_env = None  # type: ignore[assignment]
    update_job = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def handle_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Intercepts conversation turns during first-time GKE environment bootstrap.

    Deterministically assesses exact active state (`completed vs scanning`) right inside
    Python directly from file system flags, completely isolating the model from any
    file presence checking or conditional branching logic during its opening chat turns.
    """
    if not kwargs.get("is_first_turn", False):
        return None

    # Exclude background scheduled cron routines from receiving user chat onboarding injections
    platform_name = str(kwargs.get("platform", "")).lower()
    session_id = str(kwargs.get("session_id", ""))
    if platform_name == "cron" or session_id.startswith("cron_"):
        return None

    bootstrap_completed_marker = Path("/opt/data/.bootstrap_completed")
    if bootstrap_completed_marker.exists():
        # Standard daily operational state after onboarding has fully concluded
        return None

    # Check that onboarding files exist either in live /opt/data workspace or default template root
    onboarding_data_dir = Path("/opt/data/onboarding")
    onboarding_defaults_dir = Path("/opt/defaults/onboarding")
    if not onboarding_data_dir.exists() and not onboarding_defaults_dir.exists():
        return None

    # Record deterministic alignment interaction right immediately when user engages during onboarding
    try:
        Path("/opt/data/.user_aligned").touch(exist_ok=True)
        logger.info("Created /opt/data/.user_aligned coordination marker on initial user interaction.")
    except Exception as e:
        logger.warning("Notice: could not touch /opt/data/.user_aligned right in hook: %s", e)

    # Automatically bind bootstrap-inventory-scan delivery destination directly to THIS active chat space
    try:
        if get_session_env is not None and update_job is not None:
            current_platform = get_session_env("HERMES_SESSION_PLATFORM") or str(kwargs.get("platform") or "")
            current_chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
            current_thread_id = get_session_env("HERMES_SESSION_THREAD_ID")

            if current_platform and current_chat_id and current_platform.lower() != "cron":
                origin_data = {
                    "platform": current_platform,
                    "chat_id": str(current_chat_id)
                }
                if current_thread_id:
                    origin_data["thread_id"] = str(current_thread_id)

                update_job("bootstrap-inventory-scan", {
                    "deliver": "origin",
                    "origin": origin_data
                })
                logger.info("Dynamically linked bootstrap-inventory-scan target directly right to origin chat: %s (chat_id=%s)", current_platform, current_chat_id)
    except Exception as e:
        logger.warning("Could not update bootstrap-inventory-scan origin target: %s", e)

    inventory_file = Path("/opt/data/INVENTORY.md")

    if inventory_file.exists():
        # Scan completed state: directly inject completed instructions and exact INVENTORY content
        scan_completed_path = onboarding_data_dir / "scan_completed.md"
        if not scan_completed_path.exists():
            scan_completed_path = onboarding_defaults_dir / "scan_completed.md"

        try:
            instructions = scan_completed_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Could not load scan_completed.md: %s", e)
            instructions = "Present completed GKE environment scan findings directly right away."

        try:
            inventory_content = inventory_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Could not load INVENTORY.md during pre_llm_call: %s", e)
            inventory_content = "[Error reading /opt/data/INVENTORY.md. Inspect file directly.]"

        logger.info("Injecting deterministic scan_completed onboarding instructions and findings.")
        context = (
            f"\n\n[SYSTEM ONBOARDING INSTRUCTIONS — SCAN COMPLETED]\n{instructions}\n\n"
            f"--- EXCLUSIVE COMPLETED ENVIRONMENT INVENTORY FINDINGS ---\n{inventory_content}\n--- END FINDINGS ---\n"
        )

        # Deterministically execute onboarding self-cleanup right in Python across Case B right after loading findings straight into memory
        try:
            res = subprocess.run(
                ["python3", "/opt/data/scripts/bootstrap_cleanup.py"],
                check=False,
                capture_output=True,
                text=True,
            )
            logger.info("Executed bootstrap_cleanup.py deterministically inside hook right across Case B.")
            if res.stdout:
                logger.info("bootstrap_cleanup.py stdout:\n%s", res.stdout.strip())
            if res.stderr:
                logger.warning("bootstrap_cleanup.py stderr:\n%s", res.stderr.strip())
        except Exception as e:
            logger.warning("Notice: could not execute bootstrap_cleanup.py inside hook: %s", e)

        return {"context": context}
    else:
        # Scan in progress state: directly inject active running scan instructions
        scan_in_progress_path = onboarding_data_dir / "scan_in_progress.md"
        if not scan_in_progress_path.exists():
            scan_in_progress_path = onboarding_defaults_dir / "scan_in_progress.md"

        try:
            instructions = scan_in_progress_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Could not load scan_in_progress.md: %s", e)
            instructions = "Explain that background environment scan (bootstrap-inventory-scan) is running asynchronously right now."

        logger.info("Injecting deterministic scan_in_progress onboarding instructions.")
        context = f"\n\n[SYSTEM ONBOARDING INSTRUCTIONS — SCAN IN PROGRESS]\n{instructions}\n"
        return {"context": context}


def register(ctx: Any) -> None:
    # Runtime fix right for GoogleChatAdapter to ensure complete multi-chunk background cron delivery right without gateway truncation
    try:
        from plugins.platforms.google_chat.adapter import GoogleChatAdapter
        GoogleChatAdapter.splits_long_messages = True
        logger.info("Enabled splits_long_messages directly right on GoogleChatAdapter right for full inventory report delivery.")
    except Exception as e:
        logger.debug("Notice: could not patch GoogleChatAdapter.splits_long_messages right during hook registration: %s", e)

    ctx.register_hook("pre_llm_call", handle_pre_llm_call)
